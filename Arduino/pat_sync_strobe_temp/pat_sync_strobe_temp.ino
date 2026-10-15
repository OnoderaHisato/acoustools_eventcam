// PAT start marker LED + 10 kHz illumination PWM + DS18B20 PAT board temperatures, on one Arduino Uno.
//
// The two LEDs behave as in led_sync_marker_and_strobe_2.ino; the temperature lines are those of
// ds18b20_serial_logger.ino (read by pat_temp_logger.py on the PC):
//
//   * PAT 40 kHz detected on D2  -> marker LED on D8 lights once for SYNC_PULSE_US; it re-arms after the
//     PAT signal has been quiet for QUIET_US.
//   * illumination LED on D9      -> Timer1 hardware PWM from power-on, always running (as in _2):
//     prescaler 1, fast PWM mode 14, TOP = PWM_TOP (1599 -> 10 kHz), compare PWM_DUTY_COUNTS (800 -> ~50 %).
//   * DS18B20 sensors on D4       -> every TEMP_INTERVAL_MS one line per sensor on the USB serial port.
//
// Why the marker moved to hardware: reading one DS18B20 holds the CPU for about 11 ms (1-Wire bit slots),
// which would delay a loop()-polled marker LED by milliseconds. Here the PAT edge is an interrupt (INT0 on
// D2) that turns the marker LED on, and Timer2 turns it off after exactly SYNC_PULSE_US. The 1-Wire library
// blocks interrupts only inside each bit slot (at most about 70 us), so that is the largest extra delay the
// marker LED can get (the LED detection on the PC uses 100 us bins). The 10 kHz PWM is pure hardware and is
// not affected at all.
//
// After a reset (for example when a program opens the serial port with DTR) the marker is NOT armed until
// PAT has been quiet for QUIET_US, so a reset in the middle of a recording never makes an extra marker flash.
// (led_sync_marker_and_strobe_2.ino arms at once.) pat_temp_logger.py opens the port without a reset.
//
// Board: Arduino Uno / Nano (ATmega328P) only - the pins below are fixed by the timers used.
// Timer0 (millis/micros) is untouched. Timer1 and Timer2 are taken: no analogWrite on D3/D9/D10/D11,
// no Servo, no tone().
//
// Libraries (Arduino IDE > Library Manager): OneWire (Paul Stoffregen), DallasTemperature (Miles Burton).
//
// Wiring
//   D2  <- PAT 40 kHz detection input (divided / protected, as before)
//   D8  -> marker LED (as before)
//   D9  -> PT4115 DIM / MOSFET gate (as before)
//   D4  <- DS18B20 data (yellow) of every sensor, in parallel, with ONE 4.7 kOhm from D4 to 5V;
//          red -> 5V, black -> GND (3 wires, no parasite power)
//
// Serial output (115200 baud), unchanged from ds18b20_serial_logger.ino:
//   T,<cycle>,<millis_ms>,<rom_id>,<temp_C>,<status>     status: ok | disconnected | power_on_reset
//   lines starting with '#' are information. Send 's' to rescan the 1-Wire bus.

#include <OneWire.h>
#include <DallasTemperature.h>

#if !defined(__AVR_ATmega328P__)
#error "This sketch drives INT0, Timer1 and Timer2 directly: Arduino Uno / Nano (ATmega328P) only."
#endif

// ===== Pins (D2 = INT0, D8 = PB0, D9 = OC1A are fixed by the hardware used) =====
const byte START_PIN     = 2;   // PAT 40 kHz detection input
const byte SYNC_LED_PIN  = 8;   // marker LED
const byte POWER_LED_PIN = 9;   // PT4115 DIM / MOSFET gate (OC1A)
const byte ONE_WIRE_PIN  = 4;   // DS18B20 data

// ===== Marker LED =====
const unsigned int SYNC_PULSE_US = 500;       // 0.5 ms (4 .. 1020 us, 4 us steps)
const unsigned long QUIET_US     = 50000UL;   // re-arm after 50 ms without PAT

// ===== Illumination PWM (same values as led_sync_marker_and_strobe_2.ino) =====
// 16 MHz, Timer1 prescaler 1, fast PWM, TOP = ICR1: frequency = 16 MHz / (PWM_TOP + 1)
const unsigned int PWM_TOP = 1599;           // 10 kHz
// const unsigned int PWM_TOP = 1199;        // 15 kHz
// const unsigned int PWM_TOP = 799;         // 20 kHz
const unsigned int PWM_DUTY_COUNTS = 800;    // about 50 % duty
// const unsigned int PWM_DUTY_COUNTS = 600;
// const unsigned int PWM_DUTY_COUNTS = 400;

// ===== DS18B20 =====
const uint8_t RESOLUTION_BITS = 12;            // 0.0625 C, 750 ms per conversion
const unsigned long TEMP_INTERVAL_MS = 1000;   // one reading of all sensors per second
const uint8_t MAX_SENSORS = 8;

static_assert(SYNC_PULSE_US >= 4 && SYNC_PULSE_US <= 1020, "SYNC_PULSE_US must be 4..1020 us (Timer2, 4 us steps)");
static_assert(PWM_DUTY_COUNTS <= PWM_TOP, "PWM_DUTY_COUNTS must not exceed PWM_TOP");

// ===== Shared with the interrupts =====
volatile bool armed = false;     // marker LED fires at the next PAT edge
volatile bool edgeSeen = false;  // a PAT edge since loop() last looked

// ===== loop() state =====
bool patSeen = true;             // true at boot: arm only after QUIET_US without PAT
unsigned long lastSeenUs = 0;

OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature sensors(&oneWire);
DeviceAddress addresses[MAX_SENSORS];
uint8_t sensorCount = 0;

enum TempState : uint8_t { TEMP_IDLE, TEMP_WAIT, TEMP_READ };
TempState tempState = TEMP_IDLE;
unsigned long lastRequestMs = 0;
unsigned long conversionWaitMs = 770;
uint8_t readIndex = 0;
unsigned long cycle = 0;

// ---------------------------------------------------------------- illumination PWM (Timer1)

void setupPowerLedPwm() {
  pinMode(POWER_LED_PIN, OUTPUT);
  digitalWrite(POWER_LED_PIN, LOW);

  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1 = 0;

  // Fast PWM mode 14 (TOP = ICR1), OC1A (D9) non-inverting
  TCCR1A = _BV(COM1A1) | _BV(WGM11);
  TCCR1B = _BV(WGM13) | _BV(WGM12);

  ICR1 = PWM_TOP;
  OCR1A = PWM_DUTY_COUNTS;

  TCCR1B |= _BV(CS10);  // prescaler 1: start
}

// ---------------------------------------------------------------- marker LED (Timer2 one-shot)

void setupSyncTimer() {
  TCCR2A = _BV(WGM21);                        // CTC
  TCCR2B = 0;                                 // stopped
  OCR2A = (uint8_t)(SYNC_PULSE_US / 4 - 1);   // prescaler 64: 4 us per tick
  TIMSK2 = 0;
}

// Call with interrupts off.
inline void fireSyncPulse() {
  PORTB |= _BV(PB0);         // D8 HIGH now
  GTCCR = _BV(PSRASY);       // restart the Timer2 prescaler for an exact length
  TCNT2 = 0;
  TIFR2 = _BV(OCF2A);
  TIMSK2 = _BV(OCIE2A);
  TCCR2B = _BV(CS22);        // Timer2 prescaler 64
}

ISR(TIMER2_COMPA_vect) {
  PORTB &= ~_BV(PB0);        // D8 LOW
  TCCR2B = 0;
  TIMSK2 = 0;
}

// ---------------------------------------------------------------- PAT edge (INT0 on D2, rising)

ISR(INT0_vect) {
  edgeSeen = true;
  if (armed) {
    armed = false;
    fireSyncPulse();
  }
}

// ---------------------------------------------------------------- DS18B20

void printAddress(const DeviceAddress address) {
  for (uint8_t i = 0; i < 8; ++i) {
    if (address[i] < 16) {
      Serial.print('0');
    }
    Serial.print(address[i], HEX);
  }
}

void scanBus() {
  sensors.begin();
  sensors.setWaitForConversion(false);  // request now, read after the wait, without blocking
  sensorCount = 0;
  uint8_t found = sensors.getDeviceCount();
  for (uint8_t i = 0; i < found && sensorCount < MAX_SENSORS; ++i) {
    if (sensors.getAddress(addresses[sensorCount], i)) {
      sensors.setResolution(addresses[sensorCount], RESOLUTION_BITS);
      ++sensorCount;
    }
  }
  conversionWaitMs = sensors.millisToWaitForConversion(RESOLUTION_BITS) + 20;
  tempState = TEMP_IDLE;

  Serial.println(F("# ds18b20_serial_logger v3 (pat_sync_strobe_temp)"));
  Serial.print(F("# config,pwm_top,"));
  Serial.print(PWM_TOP);
  Serial.print(F(",pwm_duty_counts,"));
  Serial.print(PWM_DUTY_COUNTS);
  Serial.print(F(",sync_pulse_us,"));
  Serial.println(SYNC_PULSE_US);
  Serial.print(F("# sensors,"));
  Serial.println(sensorCount);
  Serial.print(F("# parasite_power,"));
  Serial.println(sensors.isParasitePowerMode() ? 1 : 0);
  for (uint8_t i = 0; i < sensorCount; ++i) {
    Serial.print(F("# rom,"));
    Serial.print(i);
    Serial.print(',');
    printAddress(addresses[i]);
    Serial.println();
  }
  if (found > MAX_SENSORS) {
    Serial.print(F("# warning,more sensors than MAX_SENSORS: "));
    Serial.println(found);
  }
}

void printReading(uint8_t index) {
  float temperatureC = sensors.getTempC(addresses[index]);  // about 11 ms on the 1-Wire bus
  const char *status = "ok";
  if (temperatureC == DEVICE_DISCONNECTED_C) {
    status = "disconnected";
  } else if (temperatureC == 85.0) {
    status = "power_on_reset";
  }
  Serial.print(F("T,"));
  Serial.print(cycle);
  Serial.print(',');
  Serial.print(lastRequestMs);
  Serial.print(',');
  printAddress(addresses[index]);
  Serial.print(',');
  Serial.print(temperatureC, 4);
  Serial.print(',');
  Serial.println(status);
}

// One short 1-Wire step per call, so loop() comes back often.
void serviceTemperatures() {
  unsigned long nowMs = millis();
  switch (tempState) {
    case TEMP_IDLE:
      if (nowMs - lastRequestMs >= TEMP_INTERVAL_MS) {
        sensors.requestTemperatures();  // all sensors convert at the same time (about 2 ms on the bus)
        lastRequestMs = nowMs;
        tempState = TEMP_WAIT;
      }
      break;
    case TEMP_WAIT:
      if (nowMs - lastRequestMs >= conversionWaitMs) {
        ++cycle;
        readIndex = 0;
        tempState = sensorCount > 0 ? TEMP_READ : TEMP_IDLE;
      }
      break;
    case TEMP_READ:
      printReading(readIndex);
      if (++readIndex >= sensorCount) {
        tempState = TEMP_IDLE;
      }
      break;
  }
}

// ---------------------------------------------------------------- setup / loop

void setup() {
  pinMode(START_PIN, INPUT);

  pinMode(SYNC_LED_PIN, OUTPUT);
  digitalWrite(SYNC_LED_PIN, LOW);

  // Illumination: 10 kHz / ~50 % from power-on, always (as in led_sync_marker_and_strobe_2.ino)
  setupPowerLedPwm();
  setupSyncTimer();

  Serial.begin(115200);
  scanBus();

  cli();
  lastSeenUs = micros();
  edgeSeen = false;
  armed = false;              // armed only after QUIET_US without PAT (see patSeen)
  EICRA = (EICRA & ~(_BV(ISC01) | _BV(ISC00))) | _BV(ISC01) | _BV(ISC00);  // INT0 on the rising edge
  EIFR = _BV(INTF0);
  EIMSK |= _BV(INT0);
  sei();
}

void loop() {
  unsigned long nowUs = micros();

  // 1. PAT activity (edges are caught by INT0 even while loop() was busy on the 1-Wire bus)
  if (edgeSeen) {
    cli();
    edgeSeen = false;
    sei();
    lastSeenUs = nowUs;
    patSeen = true;
  }

  // 2. re-arm the marker LED after PAT has been quiet
  if (patSeen && (nowUs - lastSeenUs > QUIET_US)) {
    cli();
    if (!edgeSeen) {
      armed = true;
      patSeen = false;
    }
    sei();
  }

  // 3. serial commands
  if (Serial.available() > 0) {
    char command = Serial.read();
    if (command == 's' || command == 'S') {
      scanBus();
    }
  }

  // 4. temperatures (one short 1-Wire step); the illumination PWM runs in Timer1 by itself
  serviceTemperatures();
}

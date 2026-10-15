// DS18B20 serial logger for the PAT board temperatures (read only; touches nothing but the 1-Wire bus).
//
// Libraries (Arduino IDE > Library Manager):
//   OneWire            by Paul Stoffregen   (the 1-Wire bus protocol)
//   DallasTemperature  by Miles Burton      (DS18B20 commands on top of OneWire)
//
// Wiring (normal power, 3 wires; do not use parasite power):
//   sensor red    (VDD)  -> 5V
//   sensor black  (GND)  -> GND
//   sensor yellow (DQ)   -> D2, plus a 4.7 kOhm pull-up resistor between D2 and 5V
//   All sensors share the same three wires. With long cables (several metres in total), try 2.2 kOhm.
//
// Output (115200 baud), one line per sensor per reading:
//   T,<cycle>,<millis_ms>,<rom_id>,<temp_C>,<status>
//   status: ok | disconnected | power_on_reset (85.0 C = read before a conversion finished, usually power)
// Lines starting with '#' are information (sensor list after a bus scan).
// Send 's' over the serial port to rescan the bus (after plugging sensors in or out).
// The PC adds its own wall-clock time when it receives a line; millis_ms is only for checking gaps.

#include <OneWire.h>
#include <DallasTemperature.h>

const uint8_t ONE_WIRE_PIN = 2;
const uint8_t RESOLUTION_BITS = 12;      // 0.0625 C steps, 750 ms per conversion
const unsigned long INTERVAL_MS = 1000;  // one reading of all sensors per second
const uint8_t MAX_SENSORS = 8;

OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature sensors(&oneWire);
DeviceAddress addresses[MAX_SENSORS];
uint8_t sensorCount = 0;

unsigned long lastRequestMs = 0;
unsigned long conversionWaitMs = 750;
bool conversionPending = false;
unsigned long cycle = 0;

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
  sensors.setWaitForConversion(false);  // request, then read after the wait, without blocking
  sensorCount = 0;
  uint8_t found = sensors.getDeviceCount();
  for (uint8_t i = 0; i < found && sensorCount < MAX_SENSORS; ++i) {
    if (sensors.getAddress(addresses[sensorCount], i)) {
      sensors.setResolution(addresses[sensorCount], RESOLUTION_BITS);
      ++sensorCount;
    }
  }
  conversionWaitMs = sensors.millisToWaitForConversion(RESOLUTION_BITS) + 20;  // small margin
  conversionPending = false;

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

void setup() {
  Serial.begin(115200);
  while (!Serial) {
    // wait for the USB serial port on boards that need it (Leonardo, Micro, ...)
  }
  Serial.println(F("# ds18b20_serial_logger v1"));
  scanBus();
}

void loop() {
  if (Serial.available() > 0) {
    char command = Serial.read();
    if (command == 's' || command == 'S') {
      scanBus();
    }
  }

  unsigned long now = millis();  // unsigned subtraction below is safe across the 49-day wrap

  if (!conversionPending && now - lastRequestMs >= INTERVAL_MS) {
    sensors.requestTemperatures();  // all sensors convert at the same time
    lastRequestMs = now;
    conversionPending = true;
    return;
  }

  if (conversionPending && now - lastRequestMs >= conversionWaitMs) {
    conversionPending = false;
    ++cycle;
    for (uint8_t i = 0; i < sensorCount; ++i) {
      float temperatureC = sensors.getTempC(addresses[i]);
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
      printAddress(addresses[i]);
      Serial.print(',');
      Serial.print(temperatureC, 4);
      Serial.print(',');
      Serial.println(status);
    }
  }
}

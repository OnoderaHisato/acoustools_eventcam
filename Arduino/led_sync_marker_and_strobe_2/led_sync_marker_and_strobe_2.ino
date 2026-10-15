// ===== Pins =====
const byte START_PIN     = 2;  // 分圧・保護後のPAT 40 kHz検出入力
const byte SYNC_LED_PIN  = 8;  // 普通LED：開始同期マーカー
const byte POWER_LED_PIN = 9;  // PT4115 DIM / MOSFET Gate へ接続。Arduino UNOではOC1A

// ===== 同期マーカーLED設定 =====
const unsigned int SYNC_PULSE_US = 500;       // 普通LEDを0.5 msだけ点灯
const unsigned long QUIET_US     = 50000UL;   // 50 ms信号が来なければ再アーム

// ===== パワーLED PWM設定 =====
// 16 MHz Arduino UNO/Nano想定。
// Timer1 prescaler = 1, Fast PWM, TOP = ICR1
// 周波数 = 16 MHz / (1 * (ICR1 + 1))
// 10 kHzにするには ICR1 = 1599
const unsigned int PWM_TOP = 1599; // 10kHz
// const unsigned int PWM_TOP = 1199; // 15kHz
// const unsigned int PWM_TOP = 799; // 20kHz
const unsigned int PWM_DUTY_COUNTS = 800;  // 約50% duty
// const unsigned int PWM_DUTY_COUNTS = 600;  // 約50% duty
// const unsigned int PWM_DUTY_COUNTS = 400;  // 約50% duty

// ===== State =====
bool armed = true;
bool patSeenSinceArm = false;

unsigned long lastSeenHighUs = 0;

// 普通LEDパルス用
bool syncLedOn = false;
unsigned long syncLedStartUs = 0;

void setupPowerLed10kHzPWM() {
  pinMode(POWER_LED_PIN, OUTPUT);

  // Timer1設定前に一度LOW
  digitalWrite(POWER_LED_PIN, LOW);

  // Timer1停止・初期化
  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1  = 0;

  // Fast PWM mode 14: TOP = ICR1
  // OC1A(D9) non-inverting PWM
  TCCR1A |= (1 << COM1A1);
  TCCR1A |= (1 << WGM11);
  TCCR1B |= (1 << WGM13) | (1 << WGM12);

  ICR1  = PWM_TOP;          // 10 kHz
  OCR1A = PWM_DUTY_COUNTS;  // 50% duty

  // prescaler = 1 でTimer1開始
  TCCR1B |= (1 << CS10);
}

void setup() {
  pinMode(START_PIN, INPUT);

  pinMode(SYNC_LED_PIN, OUTPUT);
  digitalWrite(SYNC_LED_PIN, LOW);

  // パワーLEDは起動後から常時 10 kHz / 50% duty で点滅
  setupPowerLed10kHzPWM();

  delay(100);
}

void loop() {
  unsigned long now = micros();

  // ===== 1. PAT 40 kHz信号の検出 =====
  int state = digitalRead(START_PIN);

  if (state == HIGH) {
    lastSeenHighUs = now;
    patSeenSinceArm = true;

    // 待機中なら、最初にHIGHを見た瞬間だけ普通LEDを1パルス点灯
    if (armed) {
      armed = false;

      digitalWrite(SYNC_LED_PIN, HIGH);
      syncLedOn = true;
      syncLedStartUs = now;
    }
  }

  // ===== 2. 普通LEDを0.5 ms後にOFF =====
  if (syncLedOn && (now - syncLedStartUs >= SYNC_PULSE_US)) {
    digitalWrite(SYNC_LED_PIN, LOW);
    syncLedOn = false;
  }

  // ===== 3. PAT信号が一定時間来なくなったら再アーム =====
  if (!armed && patSeenSinceArm) {
    if ((now - lastSeenHighUs) > QUIET_US) {
      armed = true;
      patSeenSinceArm = false;
    }
  }

  // パワーLEDはTimer1がハードウェアで常時PWM出力しているので、
  // loop内では何もしない。
}
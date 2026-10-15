// ===== Pins =====
const byte START_PIN      = 2;  // 分圧・保護後のPAT 40 kHz検出入力
const byte SYNC_LED_PIN   = 8;  // 普通LED：イベントカメラROI用の一発同期マーカー
const byte POWER_LED_PIN  = 9;  // LD06AJSA CEへ

// ===== 普通LED同期マーカー =====
const unsigned int SYNC_PULSE_US = 500;       // 0.5 ms
const unsigned long QUIET_US     = 50000UL;   // 50 ms信号が来なければ再アーム

// ===== PAT信号が現在来ているとみなす保持時間 =====
// 40 kHz周期は25 usなので、1 ms見えなければ停止扱いで十分
const unsigned long PAT_HOLD_US = 1000UL;

// ===== 3W LEDストロボ設定 =====
const unsigned long STROBE_PERIOD_US = 10000;  // 500 Hz
// const unsigned long STROBE_PERIOD_US = 2000;  // 500 Hz
// const unsigned long STROBE_PERIOD_US = 1000;  // 1 kHz
// const unsigned long STROBE_PERIOD_US = 100;  // 10 kHz
// const unsigned long STROBE_PERIOD_US = 50;  // 20 kHz
// const unsigned long STROBE_PERIOD_US = 25;  // 40 kHz
// const unsigned long STROBE_ON_US     = 100;   // 100 us ON
const unsigned long STROBE_ON_US     = 5000;   // 50 us ON
// const unsigned long STROBE_ON_US     = 50;   // 50 us ON
// const unsigned long STROBE_ON_US     = 25;   // 25 us ON
// const unsigned long STROBE_ON_US     = 12;   // 12 us ON
// const unsigned long STROBE_ON_US     = 1000;   // 1000 us ON

// LD06AJSAはCE HIGHでON
const bool POWER_LED_ACTIVE_HIGH = true;

// PATが来ている間だけ3W LEDをストロボさせる
const bool STROBE_ONLY_WHEN_PAT_ACTIVE = true;

// ===== State =====
bool armed = true;
bool patSeenSinceArm = false;

unsigned long lastSeenHighUs = 0;

bool syncLedOn = false;
unsigned long syncLedStartUs = 0;

bool powerLedOn = false;
bool strobeWasEnabled = false;
unsigned long strobeCycleStartUs = 0;

void setPowerLed(bool on) {
  powerLedOn = on;

  if (POWER_LED_ACTIVE_HIGH) {
    digitalWrite(POWER_LED_PIN, on ? HIGH : LOW);
  } else {
    digitalWrite(POWER_LED_PIN, on ? LOW : HIGH);
  }
}

void setup() {
  pinMode(START_PIN, INPUT);

  pinMode(SYNC_LED_PIN, OUTPUT);
  digitalWrite(SYNC_LED_PIN, LOW);

  pinMode(POWER_LED_PIN, OUTPUT);
  setPowerLed(false);  // LD06AJSA OFF

  delay(100);
}

void loop() {
  unsigned long now = micros();

  // ===== 1. PAT 40 kHz信号の検出 =====
  int state = digitalRead(START_PIN);

  if (state == HIGH) {
    lastSeenHighUs = now;
    patSeenSinceArm = true;

    // 最初のHIGH検出時だけ、普通LEDを0.5 ms点灯
    if (armed) {
      armed = false;

      digitalWrite(SYNC_LED_PIN, HIGH);
      syncLedOn = true;
      syncLedStartUs = now;
    }
  }

  // ===== 2. 普通LEDを非ブロッキングでOFF =====
  if (syncLedOn && (now - syncLedStartUs >= SYNC_PULSE_US)) {
    digitalWrite(SYNC_LED_PIN, LOW);
    syncLedOn = false;
  }

  // ===== 3. PAT信号が止まったら再アーム =====
  if (!armed && patSeenSinceArm) {
    if (now - lastSeenHighUs > QUIET_US) {
      armed = true;
      patSeenSinceArm = false;
    }
  }

  // ===== 4. PATが現在アクティブか判定 =====
  bool patActive = patSeenSinceArm && ((now - lastSeenHighUs) <= PAT_HOLD_US);

  // ===== 5. 3W LEDストロボ =====
  bool strobeEnabled = STROBE_ONLY_WHEN_PAT_ACTIVE ? patActive : true;

  if (strobeEnabled) {
    if (!strobeWasEnabled) {
      strobeCycleStartUs = now;
      strobeWasEnabled = true;
    }

    if (now - strobeCycleStartUs >= STROBE_PERIOD_US) {
      strobeCycleStartUs += STROBE_PERIOD_US;

      // 大きく遅れた場合の保険
      if (now - strobeCycleStartUs >= STROBE_PERIOD_US) {
        strobeCycleStartUs = now;
      }
    }

    bool shouldBeOn = ((now - strobeCycleStartUs) < STROBE_ON_US);

    if (shouldBeOn != powerLedOn) {
      setPowerLed(shouldBeOn);
    }

  } else {
    strobeWasEnabled = false;

    if (powerLedOn) {
      setPowerLed(false);
    }
  }
}
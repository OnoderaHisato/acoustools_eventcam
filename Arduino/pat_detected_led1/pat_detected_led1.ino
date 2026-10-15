const byte START_PIN = 2;   // 分圧後の40 kHz入力
const byte LED_PIN   = 9;   // 同期LED

const unsigned int PULSE_US = 500;        // LED点灯時間 0.5 ms
const unsigned long QUIET_US = 50000UL;   // 50 ms 信号が来なければ再アーム

bool armed = true;
unsigned long lastSeenHighUs = 0;

void setup() {
  pinMode(START_PIN, INPUT);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  delay(100);
  lastSeenHighUs = micros();
}

void loop() {
  unsigned long now = micros();
  int state = digitalRead(START_PIN);

  // 40 kHz信号が来ている間は、HIGHを頻繁に観測する
  if (state == HIGH) {
    lastSeenHighUs = now;

    // 待機中なら、最初にHIGHを見た瞬間に1回だけLEDを光らせる
    if (armed) {
      armed = false;

      digitalWrite(LED_PIN, HIGH);
      delayMicroseconds(PULSE_US);
      digitalWrite(LED_PIN, LOW);
    }
  }

  // 信号が一定時間来なくなったら、次の試行に備えて再アーム
  if (!armed) {
    if ((now - lastSeenHighUs) > QUIET_US) {
      armed = true;
    }
  }
}
# PAT 板の温度の自動記録（DS18B20＋Arduino）

非接触温度計で手入力していた上下の PAT の温度を、DS18B20 で自動記録します。
電源（`pwr01_logger.py`）や室温（`tempodisc_logger.py`）のロガーと同じ形です。

## 構成

| ファイル | 役割 |
|---|---|
| `ds18b20_serial_logger/ds18b20_serial_logger.ino` | Arduino のスケッチ。1 秒ごとに全センサーを読み、USB シリアルへ出す |
| `pat_temp_logger.py` | PC 側のロガー。受信時刻を付けて CSV に追記する |
| `pat_temp_run_link.py` | run ごとの切り出しと要約 |
| `pat_temp_logging.ps1` | 一括スクリプトからの起動・停止 |
| `pat_temp_sensors.example.json` | センサーの名前・上下・補正値の見本 |
| `test_pat_temp.py` | 実機なしのテスト |

## スケッチは 2 種類

| スケッチ | 使う場面 | DS18B20 のデータ線 |
|---|---|---|
| `Arduino/pat_sync_strobe_temp/pat_sync_strobe_temp.ino` | **計測用の Arduino に書く統合版**。開始 LED・照明のストロボ・温度を 1 台で行う | D4 |
| `ds18b20_serial_logger/ds18b20_serial_logger.ino` | 温度だけを別の Arduino で測るとき | D2 |

統合版は、`Arduino/led_sync_marker_and_strobe_2/led_sync_marker_and_strobe_2.ino`（ユーザーが正しい版と確認、2026-10-15）と同じピン（D2 入力、D8 開始 LED、D9 照明）と同じ設定値を使います。古い `led_sync_marker_and_strobe.ino`（`micros()` で点滅させる版）は、10 kHz・デューティ 50 % で点滅しなかったので使いません。

- **照明は v2 と同じ Timer1 の PWM です。** 分周 1、fast PWM（TOP = `PWM_TOP` 1599 で 10 kHz）、比較値 `PWM_DUTY_COUNTS` 800（約 50 %）で、電源を入れた直後から常に出ます。ハードウェアだけで動くので、温度の読み取りの影響を受けません。
- **開始 LED は割り込みで点灯します。** DS18B20 を 1 個読むと CPU が約 11 ms 止まるため、v2 の作り（`loop()` で D2 を見る）のままでは、開始 LED が最大十数 ms 遅れます。統合版は D2 の立ち上がりで割り込み（INT0）が入り、その場で D8 を点けます。消すのは Timer2 で、長さは 500 µs ちょうどです。
- **温度の読み取りが足す遅れは、開始 LED で最大 70 µs です。** 1-Wire のライブラリが 1 ビットの間だけ割り込みを止めるためです。PC 側の LED 検出は 100 µs ごとの集計なので、検出の時刻にはほぼ影響しません。
- **リセット直後は、PAT が 50 ms 止まるまで開始 LED を光らせません**（v2 はすぐ光らせる）。計測中に Arduino がリセットされても、記録の途中で余計な開始 LED が出ません。
- Timer1・Timer2 を使うので、D3・D9・D10・D11 の `analogWrite`、Servo、`tone()` は使えません。Uno と Nano（ATmega328P）専用です。

設定値（`PWM_TOP`、`PWM_DUTY_COUNTS`、開始 LED の長さ）は統合版の先頭にあります。v2 の値を変えたときは、統合版にも同じ値を書いてください。

## 準備

1. Arduino IDE のライブラリマネージャーで `OneWire`（Paul Stoffregen）と `DallasTemperature`（Miles Burton）を入れる。
2. 配線は 3 本線の通常給電。赤を 5V、黒を GND、黄をデータ線のピン（統合版は D4、温度だけの版は D2）につなぎ、そのピンと 5V の間に 4.7 kΩ を入れる。
3. スケッチを書き込み、シリアルモニター（115200 bps）で `T,...` の行が出ることを確かめる。**シリアルモニターは閉じてから**ロガーを使う（ポートを取り合うため）。
4. PC から確かめる。

```powershell
.\venv\Scripts\python.exe .\pat_temp_logger.py --list
.\venv\Scripts\python.exe .\pat_temp_logger.py --once
```

5. `--once` が出した ROM ID を使って、`pat_temp_sensors.example.json` を `pat_temp_sensors.json` に写して書き換える。`group` は `top` か `bottom`。上下の平均と温度の問いの列に使う。

## PAT の制御基板は開かない

PAT の制御基板は FTDI FT232H（VID 0403、PID 6014）のシリアル機器です。2026-10-14 の時点では COM5 と COM6 でした。
ロガーは FTDI のポートを自動では選ばず、FT232H は `--port` で名前を指定しても拒否します。
自動で選ぶのは Arduino 系の USB チップ（Arduino、CH340、CP210x など）が 1 つだけのときです。

Arduino へ送るのは、バスの探し直しの `s` だけです。

### Arduino の再起動・ポートの開き直しの記録

照明の PWM と開始 LED は温度と同じ Arduino で動くので、計測中に Arduino が再起動すると、その run の照明と LED 検出に影響し得ます。そのため次を記録します。

- ロガーの CSV の `event` 列: `port_opened`（開いても Arduino は動き続けた）、`port_opened_reset`（開いた瞬間に再起動した）、`port_lost`（ポートを失った。5 s ごとに開き直す）。
- run の要約（manifest と session JSON の各 run の `pat_temperature`）:
  - `arduino_restarts`: run の時間帯にスケッチの `cycle` が戻った時刻（リセット、USB 断、電源のどれでも捉える）
  - `port_events`: run の時間帯の上の `event`
  - `reading_gaps`: 10 s 以上読めなかった行
  - `interrupted`: 再起動、`port_lost`、`port_opened_reset`、読めない行のどれかがあれば True（リセットなしの `port_opened` だけでは True にしない）
- session JSON の `pat_temp_interrupted_runs`: `interrupted` だった run の番号・label・フォルダの一覧。解析ではここに載った run を除外できます。計測中は `[AUTO][PATTEMP][WARN]` も出ます。

`event` 列の無い古い CSV もそのまま読めます。Uno は USB から給電しているので、USB が抜けると開き直しとは関係なく照明も止まります。

ロガーと画面は、ポートを開くときに DTR と RTS を切り替えません。Uno はふつう、ポートを開くとリセットされますが、それを避けて、計測中にストロボが止まらないようにするためです。リセットされない場合はスケッチが何も出さないので、ロガーが 2.5 s 後に `s` を送ってセンサーの一覧を読みます。10-16 の実機では、ロガーを止めてすぐ開き直しても、スケッチの `cycle` が途切れずに続き、Arduino は再起動しませんでした。
それでも照明が途切れる場合の予備策は、Uno の RESET と GND のあいだに 10 µF を入れることです（スケッチを書き込むときは外す）。

## センサーどうしのずれ（補正値）

工場の精度は ±0.5 °C です。上下の差を比べるなら、最初に一度ずれを測ります。

1. 全センサーを束ねて、室温の静かな場所に 30 分置く（防水型なら氷水でもよい）。
2. `--output` で記録し、最後の 10 分の平均を比べる。
3. 基準（全体の平均、または信頼できる温度計）との差の符号を逆にして `offset_C` に書く。

CSV には補正前の `temp_raw_C`、使った `offset_C`、補正後の `temp_C` が並ぶので、あとから補正値を直せます。

## 自動計測との連携

`--pat-temp-log <csv>` を自動計測の入口に渡すと、次が残ります。

- 各 run に `pat_temperature_log.csv`（run の前後 5 s を含む）。
- manifest と session JSON の各 run に `pat_temperature`。センサーごとの平均・最小・最大・最初・最後と、上下の平均（`top_mean_C`、`bottom_mean_C`）、`stale`。
- session JSON の `pat_temp_log`。
- 温度の問いの CSV に `pat_sensor_top_C`、`pat_sensor_bottom_C`、`pat_sensor_age_s`、`pat_sensor_values`。

センサーの値があるときは、温度の問いに空の Enter で答えても、時間切れでも、センサーの値だけで記録が残ります。
手入力の列 `T_top_PAT_C`／`T_bottom_PAT_C` は非接触温度計の値のまま分けてあるので、最初のセッションで両方を並べて比べられます。
比べ終えたら `-NoThermalPrompts` で問いを止めても、温度は run ごとに残ります。

`run_extra3_20261012.ps1`・`run_session2_20261011.ps1`・`run_cooling_remeasure_15v.ps1` は既定でロガーを起動します。

- `-PatTempPort COM12` でポートを指定する（既定 `auto`）。
- `-PatTempSensorMap <json>` で対応表を指定する（既定はあれば `pat_temp_sensors.json`）。
- `-NoPatTempSensor` で無効にする。

Arduino が応答しなければ、警告を出して温度の記録なしで進みます。
記録中に 10 s 読めなければ CSV に `error` の行を書き、ポートを失ったら 5 s ごとに開き直します。

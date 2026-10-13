# Tempo Disc（室温・湿度・気圧）の自動記録とモニター画面

Blue Maestro の Tempo Disc（研究室の本体: 名前 `F9EE9FFC`、アドレス `F9:EE:9F:FC:F9:75`、
bmLogger の表示は v27 = Tempo Disc THPD）から、室温・湿度・気圧を PC の Bluetooth で直接読みます。
スマホのアプリや手入力は要りません。

## しくみ

- Tempo Disc は、最新の値を Bluetooth の広告パケットに載せて流し続けている（メーカー ID `0x0133`）。
  PC はそれを**受けるだけ**で、接続もペアリングもしない。本体の設定や記録は変わらない。
- 広告の中身（v27）: 電池 %、記録間隔、記録数、温度 ×10、湿度 ×10、気圧 ×10（hPa）。
  露点は、アプリと同じく温度と湿度から計算する（Magnus 式）。
- 2026-10-13 の実測: PC での受信は約 8 秒ごと。値は 10〜30 秒ごとに変わった。本体の記録間隔
  （3600 s）とは関係なく更新されるので、本体の設定を変える必要はない。
- Windows の Bluetooth は、広告と scan response（同じメーカー ID の別のデータ）を混ぜて渡す。
  `tempodisc_ble.py` は、この 2 つを分けて読む。
- **PC で記録している間は、スマホの bmLogger を閉じる**。スマホが接続している間、本体が
  広告を止める可能性がある。

## 必要なもの

- venv の `bleak`（2026-10-13 に 3.0.2 を入れた）。
- PC の Bluetooth が ON であること。

## 画面（モニター）

```powershell
.\venv\Scripts\python.exe .\tempodisc_app.py
```

- 上に、名前・型・電池・電波・最終受信。その下に、温度・湿度・露点・気圧の 4 枚のパネル
  （表示している範囲の最高・最低つき）。
- パネルをクリックすると、その量をグラフに出す。範囲は 10 分・1 時間・6 時間・24 時間・全部。
- 「● 記録開始」: 画面で受けた値を CSV に書く（形式は下のロガーと同じ）。
- 「ログを開く…」: 計測中のロガーが書いている `env_log_*.csv`（または済んだもの）を表示する。
  書き足される行も追って表示する。計測中は、こちらで見ると数字の出どころが 1 つになる。
- `--follow <csv>` で、ログ表示から始める。`--device ""` で、近くのすべての Tempo Disc を受ける。

画面は表示だけで、計測は画面を待たない。閉じても計測には影響しない。

## 記録だけ（ロガー）

```powershell
.\venv\Scripts\python.exe .\tempodisc_logger.py --once      # 1 回受けて表示（接続確認）
.\venv\Scripts\python.exe .\tempodisc_logger.py --list      # 近くの Tempo Disc をすべて表示
.\venv\Scripts\python.exe .\tempodisc_logger.py --output env.csv
```

- 値が変わったときに 1 行。変わらなくても `--interval`（既定 10 s）ごとに 1 行。
- `--stale-s`（既定 120 s）のあいだ何も受けなければ、`error` だけの行を書く（途切れが見える）。
- 列: `time_iso, time_unix_s, received_unix_s, address, name, rssi_dBm, version, battery_percent,
  log_interval_s, log_count, temperature_C, humidity_percent, pressure_hPa, dew_point_C, raw_hex,
  scan_response_hex, error`。`received_unix_s` はその値を受けた時刻。`raw_hex` と
  `scan_response_hex` は生データ（scan response の中身は読んでいない）。

## 計測との同期（自動）

`run_extra3_20261012.ps1`、`run_session2_20261011.ps1`、`run_cooling_remeasure_15v.ps1` では、
**既定で自動的に記録する**（電源のロガーと同じ形。`env_logging.ps1`）。

1. PAT を開く前に、本体を最大 30 秒探す（`--once`）。
2. 見つかれば、出力先に `env_log_<時刻>.csv` を書く別プロセスを起動する。
   **見つからなくても計測は止めず**、警告を出して環境の記録なしで進む。
3. 自動計測の入口（`--env-log`）は、各 run が終わるたびに次を残す。
   - `<run>/environment_log.csv`: run の前後 60 s を含む行。
   - `pipeline_manifest.json` の `environment`: 平均・最小・最大（温度・湿度・気圧・露点）、電池、
     最後に受けてから run の終わりまでの秒数、`stale`（5 分以上受けていない）。
   - session JSON の各 run の `environment` と、`env_log`。
4. 温度の問い（`thermal_prompt`）の記録にも、その時点の値が自動で入る
   （`sensor_T_C`、`sensor_RH_percent`、`sensor_pressure_hPa`、`sensor_dew_point_C`、`sensor_age_s`）。
   **室温と湿度は打たなくてよい**（`41.2 39.8` と上下の PAT だけで足りる）。手で打った室温・湿度は、
   従来どおり `T_room_C`／`RH_percent` に別に残る。
5. 終わったら（失敗や中断でも）ロガーを止める。

一括スクリプトのオプション:

- `-NoEnvSensor`: 環境の記録をしない（30 秒の探索も省く）。
- `-EnvDevice <名前かアドレス>`: 別の本体を使う（既定 `F9EE9FFC`）。

ほかの入口から使うときは、ロガーを自分で起動し、`--env-log <csv>` を渡す。

## ファイル

| ファイル | 役割 |
|---|---|
| `tempodisc_ble.py` | 広告の解析、露点、受信（bleak） |
| `tempodisc_logger.py` | 記録だけのロガー（CSV） |
| `tempodisc_app.py` | モニター画面（Tkinter と matplotlib） |
| `env_run_link.py` | run ごとの切り出しと要約、最新値 |
| `env_logging.ps1` | 一括スクリプトからの起動・停止 |
| `test_tempodisc.py` | Bluetooth を使わないテスト |

# PAT電源（菊水 PWR801L）の電圧・電流の自動記録

熱の計画（`THERMAL_PLAN_20260922.md`）では、記録ごとに電源の電圧・電流を控えることになっています。
これをパネルの読み取りではなく、USBで自動記録するための仕組みです（2026-09-24追加）。

## 仕組み

- `pwr01_logger.py` が電源へ**読み取りだけ**の問い合わせを送り、測定値をCSVへ1秒ごとに書きます。
  - 送るのは `MEAS:ALL?`（測定電流, 測定電圧）、`OUTP?`（出力のON/OFF）、`VOLT?`／`CURR?`（設定値）、
    `*IDN?`、`OUTP:PROT:WDOG?` と、パネルを手元の操作へ戻す `SYST:COMM:RLST LOC` だけです。
    **電圧や出力を変えるコマンドは送れない作り**になっています（許可リスト以外は送信前に拒否）。
  - 問い合わせを受けるとPWR-01はリモート状態になり、LOCAL以外のキーがロックされます。そのため
    読むたびに `SYST:COMM:RLST LOC` でパネルを戻しています。計測中でもパネルで電圧を変えられます。
  - 電源の「通信監視タイマー」（`OUTP:PROT:WDOG`、工場出荷時はオフ）が有効になっていると、ロガーが止まった
    ときに出力が切れてしまうため、**有効ならロガーは起動しません**。
- 一括スクリプトに `-PsuUsb` を付けると、PATを開く**前**にロガーを起動し（音を出した瞬間の電流の立ち上がりも
  記録されます）、セッションが終わる（失敗・中断を含む）と止めます。
- ログは出力先フォルダの `psu_log_<時刻>.csv`（列: `time_iso, time_unix_s, voltage_V, current_A, power_W,
  output_on, voltage_set_V, current_set_A, error`）と、機器名などを書いた `psu_log_<時刻>.csv.meta.json`。
- `thermal_log_prefill.py` は、同じフォルダの電源ログを自動で見つけ、各runの `supply_V`／`supply_A` を
  **そのrunの間の平均値**で埋めます。1行目の備考に「音を出した直後の電流」を、出力がONからOFFに変わった
  記録があれば `last_trip_at` を入れます。手で埋めるのは温度・湿度・粒子交換だけになります。

## 最初の準備（1回だけ、ご自身で）

1. **VISAライブラリを入れる**: 菊水の [KI-VISA](https://global.kikusui.co.jp/drivers/)（無料、x64版）を
   インストールします（管理者権限が必要）。USBTMCドライバも一緒に入ります。NI-VISAやKeysight VISAでも構いません。
2. **USBで接続**: 電源背面のUSB端子のカバーを外し、PCとつなぎます。USBは工場出荷時に有効です
   （無効なら CONFIG → CF41 を ON にして再起動）。
3. **見えるか確認**（読み取りのみ。出力は変わりません）:

   ```powershell
   .\venv\Scripts\python.exe .\pwr01_logger.py --usb --list
   .\venv\Scripts\python.exe .\pwr01_logger.py --usb --once
   ```

   `USB0::0x0B3E::0x104A::<製造番号>::INSTR  (PWR-01 800 W (PWR801L))` と、電圧・電流の1回分が表示されればOKです。

PythonのパッケージPyVISA（`pyvisa`）はプロジェクトのvenvへ導入済みです。

## 計測での使い方

いつものコマンドに `-PsuUsb` を足すだけです。

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_15v_session.ps1 -PsuUsb
powershell -ExecutionPolicy Bypass -File .\run_scaleup_20260924.ps1 -PsuUsb
powershell -ExecutionPolicy Bypass -File .\run_thermal_hold_test.ps1 -SupplyVoltage 15 -PsuUsb
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "K,WXZ" -SupplyVoltage 15 -PsuUsb
```

- 記録の間隔は `-PsuIntervalSec`（既定1秒、最小0.1秒。電源は25 msごとに電圧と電流を交互に更新します）。
- 電源が複数つながっている場合は `-PsuResource "USB0::0x0B3E::0x104A::<製造番号>::INSTR"` で指定します。
- LANでつなぐ場合は `-PsuHost <IPアドレス>`（SCPI-RAW、ポート5025。VISAは不要）。
- 起動時の接続確認に失敗すると、**PATを開く前に止まります**。電源ログ無しで撮るときは `-Psu…` を付けないでください。
- ロガーだけを単独で動かすこともできます:
  `.\venv\Scripts\python.exe .\pwr01_logger.py --usb --output .\psu_log_manual.csv`（Ctrl+Cで停止）

終わったら、記録用紙の下書きは従来どおりです（電源の列が自動で埋まります）。

```powershell
.\venv\Scripts\python.exe .\thermal_log_prefill.py .\stereo_acoustools_3d_records_V15
```

## 軌道計測との連携（各runへの同梱、落ちたときの検査、定常の判定）

`-PsuUsb` で撮ると、記録の入口に `--psu-log` が渡され、**runが終わるたびに**次が自動で行われます。
電源へは問い合わせず、ロガーが書いているCSVを読むだけなので、電源側で何かあっても撮影は止まりません。

- **runフォルダに `supply_log.csv`**: そのrunの時間帯（前後5秒を含む）の電源の記録を切り出して置きます。
  Miyabiへ送るのはrunフォルダ単位なので、電源の記録も一緒に届きます。
- **`pipeline_manifest.json` に `supply`**: 電圧・電流の平均・最小・最大・標準偏差、出力が終始ONだったか、
  音を出してからの経過（分）、そして**異常のイベント**と `suspicious`（異常あり）の印。
  session JSONの各runにも同じ要約が入ります（**記録に失敗して消えたrunの分も残ります**）。
- **異常の検出**（`[AUTO][PSU][WARN]` として画面にも出ます）:
  - `current_drop`: **出力がONのまま**、電流が直前30秒の中央値の半分未満に落ちた。PAT基板のヒューズが働くと、
    電源の出力は切れずに電流だけが下がるため、これで捉えます。
  - `output_off`: 電源の出力がONからOFFになった。
  - `log_gap`: 記録が5秒以上途切れた（USBの不調など）。`read_error` は読み取りの失敗。

セッションが終わると `psu_report_<時刻>.png`／`.json` を自動で作ります（手で作るなら
`.\venv\Scripts\python.exe .\psu_log_report.py .\stereo_acoustools_3d_records_V15`）。

- 電流と電圧を「音を出してからの分」に対して描き、撮ったrunの時間帯を青（失敗は赤）で塗り、異常の時刻に縦線を引きます。
- **定常の判定**: 5分ごとの平均電流について、(1) **最後の30分の変化率**と、(2) **それ以降ずっと最終値の±1%に
  収まった時刻（何分後に落ち着いたか）**を出します。ヒューズが働いた前後や出力OFFの間は計算から除きます。
  熱の計画の合格条件（剛性kの±5%）はkcheckから解析側が判定しますが、電流の頭打ちは板の温度が落ち着いた
  目安になります。

## このPCで分かったKI-VISAの癖（2026-09-24、対処済み）

- **インストール直後、それ以前から開いているターミナルやアプリからは使えない**: インストーラが設定する環境変数
  （`VXIPNPPATH` など）とPATHが、既に動いているプロセスには届かないため（VISAの初期化が
  `VI_ERROR_INV_OBJECT` で失敗する）。ロガーはWindowsの設定からこれらを読み直すので、再起動は不要です。
- **KI-VISA 5.5.0.275（x64）は、セッション番号が 2^31 以上だと必ず失敗し、未満なら必ず成功する**（ルーター
  `visa64.dll`／`visa32.dll` 経由でも、`kivisa32.dll` を直接使っても同じ。番号はプロセスごとにランダムで、
  同じプロセスの中では変わらない）。ロガーは番号が悪いと子プロセスで開き直すので、利用者からは見えません
  （最大20回。実測では1〜数回で成功）。一度つながったプロセスは、その後ずっと使えます。
- 一括スクリプトの停止処理は、子プロセスごと確実に止めます。
- このPCのPWR801Lは `USB0::0x0B3E::0x104A::CU000603::0::INSTR`（ファームウェア VER01.25 BLD0057）。

## 参考

- 通信仕様は PWR-01 Interface Manual（`MEAS:ALL?` p.81、USB p.23--24、SCPI-RAWのポート5025、
  `SYST:COMM:RLST` p.175、`OUTP:PROT:WDOG` p.96）。USBのIDは VID 0x0B3E、PWR801L（800 W）は PID 0x104A。

# 15 V（電圧を下げた）熱の定常状態の計測手順

正本は解析側の `G:\マイドライブ\Experiment\20260921\measurement_plan\THERMAL_PLAN_20260922.md` §3 と、
同じフォルダの `MESSAGE_TO_ACQUISITION_PC_20260923.md` です。この手順書は、それを計測PCのスクリプトで
実行する方法だけをまとめています。

## 背景（要約）

- 18 V・上下合計 4.8 A では、約 30 分で基板上のリセッタブルヒューズが働いて止まる。熱の定常状態が無く、
  剛性 k と場 w がセッション中ずっと動くため、w の補償が再現しない。
- 15 V にすると力と発熱はおよそ 0.69 倍。熱の定常状態ができるかを保持試験（セッション1）で確かめ、
  できれば動作点の K・γ・w を取り直す（セッション2）。
- 用語: **稼働** = 軌道を流している、または粒子を浮かせて待っている（どちらも音が出ている）。
  **休止** = 電圧は掛かっているがスクリプト停止・音なし。**冷却** = 電源オフ。
  **冷えた状態** = 休止か冷却を 45 分以上続けたあと。

## スクリプトが自動でやること

- **電圧のラベル**: `-SupplyVoltage 15` を付けると、全 run のフォルダ名の末尾が `..._scale100_V15_<時刻>` になり、
  `pipeline_manifest.json` の `automation.supply_voltage_V` に 15 が入る。電源の電圧そのものは変えない（手で設定する）。
- **置き場所**: 既定の出力先は電圧ごとの `stereo_acoustools_3d_records_V15`（18 V の記録と混ざらない）。
  フォルダ名が長すぎて `_V15` が切れる場合は、開始前にエラーで止まる。
- **時刻合わせ（セッション1）**: 時計の起点は **PAT の出力が始まった瞬間（音を出した時刻）**。
  1 組目（kcheck x → y → z）は最初の preview で Enter を押した直後に撮る。
  2 組目以降は「起点から 5 分 × 組番号」ちょうどに始まる（前の組が長引いたときは、終わりしだいすぐ始める）。
  組と組のあいだは、粒子を中心に浮かせたまま待つ（稼働）。
- 各 run に `sound_on_since`（音を出した時刻）、`seconds_since_sound_on_at_run_start`、`schedule_group` が記録される。
- 撮り直し: 記録や LED 検出に失敗したら「再計測しますか？ (Y/n)」が出る（`-Unattended` なら自動で撮り直す）。
  **粒子の脱落は自動では検知しない。**

## セッション1: 保持試験（約 1.5 時間）

1. 冷えた状態にする（休止か冷却を 45 分以上）。電源を **15 V** にする。
2. まず実機を開かずに確認する:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run_thermal_hold_test.ps1 -SupplyVoltage 15 -DryRunOnly
   ```

   13 組（0, 5, …, 60 分）× x / y / z = 39 run、最後の組が 60 分であることが表示される。
   ディスクの空き（1 run 約 1.6 GB、39 run で約 62 GB）も確認され、足りなければ止まる。
3. 本番:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run_thermal_hold_test.ps1 -SupplyVoltage 15
   ```

   PAT が開いた時点で時計が始まる。粒子を浮かせ、stereo preview で確認して Enter を押す。以後は 60 分まで自動。
4. 手で控えるもの（各組の前後で）: 電源の電流（**音を出した直後の値を必ず**。目安 4.0 A）、
   板の裏の温度（上・下）、板のあいだの空気（音場の外側）、室温、湿度。温度センサは音場に入れない。
5. PAT が勝手に止まった（ヒューズが働いた）ら、その時刻を控える（`last_trip_at`）。
6. 終わったら記録用紙を作る:

   ```powershell
   .\venv\Scripts\python.exe .\thermal_log_prefill.py .\stereo_acoustools_3d_records_V15
   ```

   `stereo_acoustools_3d_records_V15\thermal_log_<時刻>.csv` に、記録開始時刻・run 名・音を出した時刻・電圧・
   「group N, t+X min」が入った行ができる。電流・温度・湿度・粒子交換を手で埋める（上書きはされない）。

合格の判定（解析側）: 60 分止まらない、電流が一定、最後の 30 分で k_x・k_z が ±5 % 以内。
不合格なら次回は 12 V で同じことをする（`-SupplyVoltage 12`。出力先は `..._records_V12` になる）。

## セッション2: 動作点での取り直し（約 1.5 時間＋3D 化）

セッション1で分かった「落ち着くまでの時間」だけ稼働させてから始める。

1. kcheck x / y / z と、w の場の 5 面走査（10 本）を 1 回の PAT セッションで:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "K,WXZ,WYZ,WXY,WXZP4,WXZM4" -SupplyVoltage 15
   ```

   `K` は「kcheck x / y / z だけ」を表す新しい設計名。13 run（12 run を超えるので警告が出るが、そのまま進んでよい）。
2. Miyabi へ転送して 3D 化する（`MIYABI_UPLOAD_JP.md`。`stereo_acoustools_3d_records_V15` も対象になる）。
   この待ち時間は稼働のままでも休止でもよい。
3. 解析側が新しい k・γ で FF の設計を作り直し、走査から `_Ws` を作る。**届いたら取り込む**（まだ無い）。
4. kcheck → OFF → C → C_Ws → C_Ws → C → OFF → kcheck を 7 Hz と 10 Hz で（手順3の指令が届いてから）。
5. `wscan_XZ_a / b` をもう一度:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "WXZ" -SupplyVoltage 15
   ```

## 14 mm検証セッション（2026-09-24追加、解析側の `README_15V_SESSION.md`）

15 Vの動作点で同定したk・γ（k_x 0.208、k_y 0.198、k_z 2.48 [1/ms²]、γ 0.0372／0.0319／0.0237 [1/ms]、
τ0 0.9 ms）で設計し直したハートとカーディオイド（幅14 mm）を、1回のPATセッション（約95分）で撮ります。

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_15v_session.ps1 -DryRunOnly
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_15v_session.ps1
```

- 冷えた状態（休止45分以上）から始め、電源は15 Vにしておきます。最初のrunだけpreviewとEnterです。
- 時計は音を出した瞬間から。各組は 5〜35分（暖機のkcheck x/z）、40分（kcheck x/y/z）、43分（走査1回目）、
  46分（ハート10 Hz 7本）、56分、58分（カーディオイド10 Hz 7本）、68分、70分（ハート7 Hz 5本）、
  77分（カーディオイド7 Hz 3本）、82分、85分（走査2回目）、95分 に始まります。合計52 run・約78 GB。
- `-SkipWarmupChecks` で5〜35分のkcheckを省略（40分まで待つだけ）、`-SkipCardioidF7` で77分の3本を省略できます。
- 新しい設計名: `_Ws4`（走査した場のなだらかな成分だけ補償）、`_Ws`（細かい構造まで補償）。
- **フォルダ名**: 長さ制限（記述部28文字）のため、run名は短縮形です。解析側が使う目印は残しています。
  例 `feedforward_validation_017_cardioid_a5p4_f10_C_Ws4_scale100_V15_<時刻>`。完全な設計名は
  `pipeline_manifest.json` の `feedforward_design.design`（例 `C_delay_inverse_Ws4`）にあります。

## 注意

- **1 回の PAT セッションで 39 run・約 65 分**になる。これまで「1 セッション 8〜12 run 以内」を目安にしていたのは、
  2026-09-16 に PAT 接続から約 16〜17 分で LED が消え、左右のイベントも激減する異常が 2 回あったため。
  これは 18 V でヒューズが働いて PAT が止まった可能性がある（未確認）。15 V で 60 分もつかどうかが、まさに
  この試験の目的なので、長いセッションのまま実施する。
- 15 V では復元力が約 0.69 倍になる。最初の組の preview で、粒子が中心で安定しているかをよく見る。
  z の kcheck（1.05 mm）で粒子が落ちたら、その時点で Ctrl+C で止める。
- LED の明るさが電圧で変わるかは未確認。最初の run の LED 検出（`[SYNC][LED]` の peak）を見ておく。
- 18 V の既定の置き場（`stereo_acoustools_3d_records_ff_heart` など）には書き込まない。

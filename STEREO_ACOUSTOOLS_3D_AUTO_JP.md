# AcousTools・ステレオイベントカメラ 3D自動計測

## Plan B追加計測（2026-08-26統合）

`G:\マイドライブ\Experiment\20260826\`の追加計測B1/B2/B3は、現行HF export経路へ
`plan_b_20260825/`として統合済みです。全51 runを10 kHzで再生成し、元資料の数値指令との
一致を確認しています。実行順、安全ゲート、context JSON、出力先は
`PLAN_B_ADDITIONAL_MEASUREMENT_JP.md`を参照してください。

Plan Bでは次を追加で保証します。

- B1はbaselineまたは単一escalation rankだけを1 hardware sessionへ選択する。
- preregistered response gateのHOLD runには専用ackを要求する。
- B2のdrive A/B/Cは全トランスデューサ振幅を1.00/0.85/0.70へ自動設定し、位相は変えない。
- B2粒子条件とB3環境条件はoperator contextをmanifestへ保存する。
- 全runでステレオpreviewとEnterを強制し、`--keep-going`を禁止する。
- B2の非Tier staircaseをPlan B専用familyとして扱い、既存Tier A--Hの制約とは分離する。

ハードウェアを開かない確認例:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\plan_b_20260825\export_B1_small_amplitude_multisine `
  --dry-run
```

## vzr同定計測（2026-09-17統合）

`G:\マイドライブ\Experiment\20260917\measurement_plan` の計画（x/y と z の同時正弦駆動で
OptiTrapの `vzr` を取る）は、`vzr_identification_20260917/` のplanとexportとして同じ
`--hf-export-dir` 経路へ統合済みです。生成器に2軸同時の `kind=multitone` を追加し、
G:の参照NPZと全サンプルで一致するexportを生成します。手順、run順序、安全ゲートは
`VZR_IDENTIFICATION_MEASUREMENT_JP.md` を参照してください。

```powershell
# 実機を開かない確認
powershell -ExecutionPolicy Bypass -File .\run_vzr_identification_all.ps1 -DryRunOnly

# 実機計測（単軸保持確認 → L1 → L2 → L3 → L4 → y単軸 → Y1 → Y2。vzr正弦波は全runでpreviewとEnter）
powershell -ExecutionPolicy Bypass -File .\run_vzr_identification_all.ps1
```

vzrのrunは `--acknowledge-vzr-protocol` を要求し、全runでステレオpreviewとEnterを強制、
`--keep-going`・無人モード・他exportとの混在を拒否します。制限超過の指令は生成時に
例外で止まり、自動縮小されません。

## FFハート検証計測（2026-09-18統合）

`G:\マイドライブ\Experiment\20260918\measurement_plan\ff_heart` の事前計算済み指令
（ハート7 mm・10 HzのOFF／A／C／OT-ident）は、`ff_heart_20260918/` のplanとexportとして同じ
`--hf-export-dir` 経路へ統合済みです。生成器の `kind=imported` がNPZの指令を数値そのまま
取り込み（内容ハッシュ固定、制限超過は例外、取得側のFF・クリップ・縮小なし）、所望軌道 r は
各runの `*_reference_log.csv` と `command_trajectory.npz` に保存されます。剛性確認用の
短いステップも同じexportにあり、`--hf-run` で挟み込めます。手順は
`FF_HEART_VALIDATION_MEASUREMENT_JP.md` を参照してください。

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -DryRunOnly
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1
```

FF検証のrunは `--acknowledge-ff-validation-protocol` を要求します。`--keep-going` と、step-response以外の
exportとの混在は拒否します。一括スクリプトの既定は2026-09-20から「最初のrunだけpreviewとEnter」です
（下の節を参照）。

## ステップ型vzr計測（2026-09-19統合）

`G:\マイドライブ\Experiment\20260918\measurement_plan\vzr_step` の計画（水平1軸とzのジャンプを
1記録に混ぜ、同時ジャンプで `vzr` を取る）は、`vzr_step_20260919/` のplanとexportとして同じ
`--hf-export-dir` 経路へ統合済みです。生成器の `kind=staircase` に、保持位置を `[x, y, z]` で並べる
`level_sequence_mm` を追加し、解析側の指令NPZと全サンプルでビット一致するexportを生成します。
安全検査は従来のstaircaseと同じ振幅のみ（中心からの距離、1サンプルの変化、脱出境界。いずれも
ベクトルの大きさ）で、実機ackは `--acknowledge-step-response-risk` です。手順は
`VZR_STEP_MEASUREMENT_JP.md` を参照してください。

```powershell
powershell -ExecutionPolicy Bypass -File .\run_vzr_step_all.ps1 -DryRunOnly
powershell -ExecutionPolicy Bypass -File .\run_vzr_step_all.ps1
```

既定は計画書どおり XS1 → XS2 → XS2 → XS2 の4 runです（run間の確認は下の節を参照）。

## 大振幅1軸ステップ（2026-09-20統合）

`G:\マイドライブ\Experiment\20260919\measurement_plan\large_step` の計画（水平の飽和長 vxr を分離して
力の上限 A_r = k/vxr を出す）は、`large_step_20260920/` のplanとexportとして同じ `--hf-export-dir` 経路へ
統合済みです。既存の `kind=staircase`（単軸）で生成し、解析側の指令NPZとビット一致します。手順は
`LARGE_STEP_MEASUREMENT_JP.md` を参照してください。

```powershell
powershell -ExecutionPolicy Bypass -File .\run_large_step_all.ps1 -DryRunOnly
powershell -ExecutionPolicy Bypass -File .\run_large_step_all.ps1
```

既定は XL12 → XL15 の2 runです。1.8／2.0 mmは推定力最大点より外から戻るので、必ず小さい段から
上げ、戻りが鈍ったら止めてください。

## 一括スクリプトのrun間確認（2026-09-20変更）

`run_ff_heart_validation.ps1`、`run_vzr_step_all.ps1`、`run_large_step_all.ps1` は、既定で**最初のrunだけ**
ステレオpreviewとEnter確認を行い、残りは自動で続けます（`--unattended-after-first-checkpoint`）。
記録やLED検出に失敗したときは、従来どおり「同じ軌道を再計測しますか？ (Y/n)」を出します
（新設の `--prompt-on-capture-failure`）。

- `-ConfirmEachRun`: 変更前と同じく、全runでpreviewとEnterを行う。
- `-Unattended`: 失敗時も尋ねず、`-AutomaticCaptureRetries`（既定2）回まで自動で撮り直す。

粒子の脱落は自動検知しません。

## 電圧を下げた熱の保持試験（2026-09-23追加）

解析側の `THERMAL_PLAN_20260922.md` §3（15 Vで熱の定常状態を作る）用に、自動計測の入口へ次を追加しました。
手順は `THERMAL_15V_MEASUREMENT_JP.md` を参照してください。

- `--supply-voltage-v V`: 全runのフォルダ名の末尾に `_V15` などを付け、`supply_voltage_V` を記録します。
  電源の電圧は手で設定します。タグが長さ制限で切れる出力先は開始前に拒否します。
- `--schedule-interval-sec S` と `--schedule-group-size N`（無人モードのみ）: N本ずつの組を、PATの出力開始
  （音を出した時刻）から S 秒 × 組番号 に開始します。組の間は粒子を中心で保持します。
- `run_thermal_hold_test.ps1 -SupplyVoltage 15`: kcheck x/y/z を5分ごとに60分（13組・39 run）。
- `run_ff_heart_validation.ps1` に `-SupplyVoltage` と、kcheckだけを表す設計名 `K` を追加しました。
- `thermal_log_prefill.py`: session JSONから記録用紙（`thermal_log_template.csv` と同じ列）の下書きを作ります。

```powershell
powershell -ExecutionPolicy Bypass -File .\run_thermal_hold_test.ps1 -SupplyVoltage 15 -DryRunOnly
```

## 14 mm検証セッションと安全上限の引き上げ（2026-09-24）

- `--schedule-offsets-sec`（無人モードのみ）: runごとに「音を出してから何秒で始めるか」をコンマ区切りで
  与えます（空欄は前のrunの直後）。間隔が不均一な時刻表用で、均等なら `--schedule-interval-sec` を使います。
  `--schedule-interval-sec` との併用は拒否します。
- `run_ff_heart_15v_session.ps1`: 15 Vの動作点で撮る14 mmの検証セッション（52 run・約95分）。
  手順は `THERMAL_15V_MEASUREMENT_JP.md` を参照してください。
- **staircaseの1ジャンプ上限を 2.144 mm（推定脱出境界 λ/4）から 3.2 mm へ引き上げました**（ユーザー承認、
  規模拡大の2.5／3.0 mmステップのため）。λ/4を超えたことは従来どおりmetadataの `escape_boundary_exceeded`
  に残り、`staircase_max_jump_limit_mm` に実際の上限を記録します。**この境界は粒子を保持できる範囲の目安で、
  超えると粒子を落とす可能性があります。**
- オフセット・速度・加速度・\|u−r\| の上限はコードではなくplanの `defaults.safety_limits` です。規模拡大の
  planでは **`max_offset_mm` 35 mm**（依頼は30 mmだったが `cardioid_a23` の実測が30.741 mmで弾かれるため、
  ユーザーの指示で35 mmへ）／3500 mm/s／350,000 mm/s²／\|u−r\| 4 mm／端点0.01 mm を使います。この値で
  20260924の指令17本すべてが検査を通ることを確認済みです。

## 規模拡大（2026-09-24、`scaleup_20260924/`）

`SCALE_UP_PLAN_20260924.md` の一式を、planとexportとして取り込みました（22 run）。26／44／60 mmの
カーディオイド（10 Hz、OFF／A_delay／C_delay／C_nl／OT_ident）、半径28 mm・24 sの走査、2.5／3.0 mmの
水平ステップ、kcheckです。取り込み指令はG:の配列とビット一致、2本のステップは解析側exportとビット一致します。

```powershell
powershell -ExecutionPolicy Bypass -File .\run_scaleup_20260924.ps1 -DryRunOnly
powershell -ExecutionPolicy Bypass -File .\run_scaleup_20260924.ps1
```

- 順番は計画書§3のとおり（kcheck → XL25 →（確認）XL30 → R28走査 → kcheck → a10の5本 → a17の5本 →
  kcheck →（確認）a23の5本 → kcheck）。
- **暖機（既定40分）**: 冷えた状態から始め、PATを開いた直後に粒子を確認してEnter、その後 `-WarmupMin`（既定40）分
  待ってから§3の順番を始めます。暖機中は5分ごとにkcheck x/zを撮ります（`-SkipWarmupChecks` で省略）。
  14 mmセッションの直後で板が温まっているときは `-WarmupMin 0` ですぐ始めます（最初のrunの前で確認）。
- `--checkpoint-run-numbers`（新規）で、無人モードでも指定したrunの前だけpreviewとEnterを入れます。
  既定では**3.0 mmステップの前**と**60 mmカーディオイドの前**で止まります。
- `-Sizes`、`-SkipLargeSteps`、`-SkipScan`、`-IncludeExtraDesigns`（A_delayとOT_identも撮る）。
- **粒子を落とす可能性があります**（60 mmで中心から30.7 mm・2890 mm/s、水平ステップは鉛直のλ/4推定の外側）。
  previewで粒子が無ければCtrl+Cで止めてください。

## 構成

3D自動計測は、単発計測と同じrecording/postprocessコアを使用します。自動化スクリプトに、粒子追跡や三角測量の実装を重複させていません。

```text
long_random_3d_patterns_initial.json
  └─ acoustools_stereo_eventcam_3d_recording_auto.py
       └─ stereo_acoustools_3d_recording_core.py
            ├─ 左右events.npz
            ├─ ideal_log.csv
            ├─ capture_start_marker.json
            ├─ pat_camera_timing.json
            └─ pipeline_manifest.json (processing_status=pending)

stereo_acoustools_3d_auto_monitor.py
  └─ pendingのpipeline_manifest.jsonを監視
       └─ stereo_acoustools_3d_postprocess_core.py
            ├─ 左右events.npz → 左右2D軌道CSV
            ├─ 左右2D軌道CSV + ステレオ校正NPZ → stereo_3d_points.npz
            └─ stereo_3d_points.npz + ideal_log
                 └─ 3D比較結果
```

## 事前入力

既存データとして読み込む必須ファイルは次の2つです。

- `long_random_3d_patterns_initial.json`
- `stereo_checkerboard_calib_extrinsics_20260805/stereo_calibration_square7p12_extrinsics_final.npz`

PAT座標系で絶対位置比較するときだけ、追加で`camera_to_pat_transform.npz`を指定します。プレビューPNGは計測入力ではありません。

## 最初に軌道を確認

ハードウェアを開かずにJSON、PAT sample rate、軌道生成を検証します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py --dry-run
```

全軌道の個別PNG、一覧PNG、統計CSVを再生成します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py --preview-only
```

出力先は`long_random_3d_patterns_initial_preview_10kHz`です。初期JSONはXYZ独立のランダム3D軌道8条件で、更新レートは10,000 Hz、各軸の指定範囲は最大±30 mmです。

## 自動recording

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py
```

動作は次のとおりです。

1. PATで粒子を中心に保持してから、左右カメラのプレビューを最初の1回だけ表示します。
2. JSON条件ごとに軌道とホログラムを準備します。
3. 左右カメラをハードウェア同期で記録します。
4. 左カメラ右上ROIのLEDイベントからPAT開始時刻を検出します。
5. 成功runだけを確定し、`processing_status=pending`のmanifestを最後に保存します。
6. 粒子を中心へ戻して次の条件へ進みます。

AcousTools/OpenMPDへの接続はauto開始時に1回だけ作成します。条件間ではPAT出力を停止せず、
粒子を中心位置で浮遊させたまま同じ接続を次のrunへ引き継ぎます。全条件の正常終了、エラー、
またはCtrl+Cによる中断時に、auto全体の終了処理でPATを1回だけ停止します。単発recordingと
一体型pipelineは従来どおり、各スクリプトの終了時に自身がPATを停止します。

通常は条件間のEnter入力を要求しません。毎回確認したい場合は`--confirm-each-run`、毎回カメラプレビューを表示したい場合は`--preview-each-run`を付けます。

最初の1条件だけを実機確認する例です。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py --limit 1
```

絶対比較用変換を使用する例です。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --camera-to-pat-transform .\PAT_GRID_SESSION\registration\camera_to_pat_transform.npz
```

デフォルトのLED条件は単発recordingと同じです。

```text
side = left
ROI  = 600,0,1280,180
```

LED検出に失敗した計測は既定で削除され、同じ条件を再計測するか確認されます。`Y`またはEnterで再計測します。デバッグ目的で失敗データを残す場合だけ`--keep-failed-captures`を使います。

## 粒子抽出前の軽量ステレオ動画確認

スパコンへ転送する前に、左右の撮影範囲へ粒子が収まっているかだけを確認する場合は、
粒子追跡・三角測量・ideal比較を実行しない軽量レンダラーを使用します。

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_quick_preview.py `
  .\stereo_acoustools_3d_records_auto
```

デフォルト設定は5 fps、1フレーム50 ms積算、各カメラ640×360、左右結合1280×360です。
PAT運動区間の前後0.25秒だけを対象にします。左カメラのPAT開始LED ROIは既定で除外されます。

このスクリプトは非圧縮NPZ内の`events.npy`をその場でmemory-mapし、低fpsフレームに必要な
時刻範囲だけを読みます。数百MBの左右イベント配列全体をRAMへ展開しません。

各runには次が作られます。

```text
quick_stereo_preview/
├─ stereo_lowrate_5fps.mp4
├─ stereo_lowrate_5fps_contact_sheet.jpg
└─ stereo_lowrate_5fps_manifest.json
```

全runの中間時刻を並べた`quick_stereo_preview_overview_5fps.jpg`と、各runの4時点を並べた
`quick_stereo_preview_all_times_5fps.jpg`もrecordingルートへ作成します。
既存動画はスキップするため再開可能です。作り直す場合は`--overwrite`を付けます。

```powershell
# 最初の1 runだけ
.\venv\Scripts\python.exe .\stereo_acoustools_3d_quick_preview.py --limit 1

# 全記録区間を10 fpsで作り直す
.\venv\Scripts\python.exe .\stereo_acoustools_3d_quick_preview.py `
  --fps 10 --accumulation-ms 50 --full-recording --overwrite
```

この確認動画は観察専用であり、`pipeline_manifest.json`の`processing_status`は変更しません。

## 高周波同定計測（70--110 Hz帯）

高周波同定ではランダム3D JSONの代わりに、事前生成した
`hf_identification_export`を同じ自動recordingへ渡します。カメラserial、left-master同期、
左LED ROI、ステレオ校正は通常計測と同じです。delay feedforwardは適用しません。

最初にハードウェアを開かず、12本の有効コマンドと安全値を確認します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\hf_identification_export `
  --dry-run
```

実験順序は固定です。

1. `00_static_baseline_r01/r02`を記録する。
2. `10_x_chirp_40_130hz_r01`だけを50% scaleで記録する。
3. 粒子が保持され、左右カメラに見えていることを軽量動画で確認する。
4. 確認後、full scaleのX/Y chirpを記録する。
5. X/Y multisineを記録する。

50% X chirpの実計測例です。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\hf_identification_export `
  --output-dir .\stereo_acoustools_3d_records_hf `
  --label 10_x_chirp_40_130hz_r01 `
  --hf-command-scale 0.5 `
  --confirm-each-run
```

50%試行を確認した後のfull scale動的計測には、必ず次の確認フラグを付けます。

```text
--acknowledge-hf-retention-and-visibility
```

このフラグがない場合、50%を超える動的HFコマンドはPATを開く前に停止します。
Z軸の`90_z_chirp_220_350hz_diagnostic`は、ノイズ床と保持を確認するまで無効のままです。

同じexportを異なるscaleで1セッションに並べる場合は、`--hf-run LABEL=SCALE`を
指定順に複数回使用します。次は50%を再取得した直後に100%を計測します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\hf_identification_export `
  --output-dir .\stereo_acoustools_3d_records_hf `
  --hf-run 10_x_chirp_40_130hz_r01=0.5 `
  --hf-run 10_x_chirp_40_130hz_r01=1.0 `
  --acknowledge-hf-retention-and-visibility `
  --confirm-each-run
```

`--hf-run`は重複するlabelを許可し、記載順を維持します。この指定は`--label`、
`--start-index`、`--limit`とは併用しません。カメラのtail marginは既定で2.0秒です。
100,000点HF runで観測された約1.2秒のPAT送信開始遅延を含めても軌道末尾が欠けない
ようにするためで、通常は`--capture-tail-margin-sec`を追加する必要はありません。

`--hf-export-dir`は複数回指定でき、指定したディレクトリ順・各manifest順に1回のPAT接続で
記録します。run名はexport間で重複できず、`--hf-run`、`--start-index`、`--limit`、Plan B
selectorとは併用できません（`--label`は可）。

step-response staircaseとFF検証の取り込み指令だけを選択した場合（混在可）は、`--unattended-after-first-checkpoint`で
最初のrunだけプレビューとEnter確認を行い、残りを無人で連続記録できます。最初のrunの
ホログラムはPAT出力前に計算します。無人区間で記録に失敗したrunは
`--automatic-capture-retries`（既定2）回まで自動で再計測し、それでも失敗すれば停止します。
粒子の脱落は自動検知しません。単一振幅系列の一括実行は
`run_step_response_single_amplitude_all.ps1`と`STEP_RESPONSE_SINGLE_AMPLITUDE_JP.md`を参照してください。

各HF runには元の`command_trajectory.npz`、`trajectory_metadata.json`、
`command_offset_log.csv`、プレビューがコピーされ、SHA-256と実際のcommand scaleが
`pipeline_manifest.json`へ記録されます。絶対PAT座標は従来どおり`ideal_log.csv`へ保存されます。

## モニター後処理

### 計測後にまとめて処理（計測負荷を優先）

自動recordingの終了後に次を実行します。

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_auto_monitor.py --once
```

既存の`pending` runをすべて処理して終了します。粒子抽出が重く、計測中のCPU・ディスク負荷を避けたい場合はこちらを推奨します。

### 2D版と同様に計測と並行して監視

先に別PowerShellでモニターを起動します。

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_auto_monitor.py --new-only
```

その後、別PowerShellで自動recordingを起動します。`--new-only`はモニター起動前から存在するrunを無視します。recording後にモニターを起動する場合は付けないでください。

モニターは次の条件をすべて満たすrunだけを処理します。

- `pipeline_manifest.json`が存在する
- `capture_complete=true`
- `processing_status=pending`
- 左右NPZ、同期マーカー、timing、ステレオ記録manifestが存在し、サイズが安定している
- 同じrunを別モニターが処理中でない

処理成功後は`processing_status=complete`になるため、モニターを再起動しても二重処理しません。`failed`または`interrupted`を再処理するときは次を使用します。

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_auto_monitor.py --once --retry-failed
```

追跡条件はrecording時にmanifestへ保存されます。モニター起動時に上書きすることもできます。

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_auto_monitor.py --once `
  --window-us 300 --hop-us 100 --roi 0,0,1280,720
```

## JSONパラメータ

追加したextended/chirped/cuspedと保持限界を1セッションで計測する統合計画は
`all_additional_3d_dataset_plan.json`です。include型JSON、条件順、安全な保持限界tail、
実行コマンドは`ALL_ADDITIONAL_3D_DATASET_JP.md`を参照してください。

3軸チャープをJSONから連続計測する場合は`Chirped_3D`を使用できます。
通常計画、保持限界診断、安全確認フラグは`CHIRPED_3D_DATASET_JP.md`を参照してください。
カスプを含むcardioid、nephroid、deltoid、astroid、hypocycloidは`Cusped_3D`です。
sharp／rounded対照と保持限界計画は`CUSPED_3D_DATASET_JP.md`を参照してください。
`risk_level=retention_boundary`の条件は、通常の拡張軌道確認に加えて
`--acknowledge-retention-boundary-risk`がなければ実機を開きません。

画面端の余裕を少し増やす95%縮小版として、
`long_random_3d_patterns_initial_scale95.json`も用意しています。seed・時間・10 kHz更新レート・
周波数構成は元JSONと同じで、XYZ範囲と速度・加速度上限だけを一律95%にしています。

さらに余裕を増やす場合は、同じ規則で縮小した次のJSONも使用できます。

- `long_random_3d_patterns_initial_scale90.json`
- `long_random_3d_patterns_initial_scale85.json`
- `long_random_3d_patterns_initial_scale80.json`

いずれも元JSONと同じ8条件、seed、時間、10 kHz更新レート、周波数構成を維持し、XYZ範囲と
速度・加速度上限だけをファイル名の割合で縮小しています。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\long_random_3d_patterns_initial_scale95.json
```

各要素は次の形式です。

```json
{
  "shape": "Long_Random_3D",
  "repeat": 1,
  "params": {
    "label": "balanced_xyz_8s_seed2101",
    "seed": 2101,
    "duration_sec": 8,
    "sample_hz": 10000,
    "x_limit_mm": 22,
    "y_limit_mm": 22,
    "z_limit_mm": 22,
    "f_min_hz": 0.25,
    "f_max_hz": 10,
    "components": 9,
    "chirps": 3,
    "waypoints": 9,
    "drift_weight": 0.35,
    "max_speed_mm_s": 900,
    "max_accel_mm_s2": 30000
  }
}
```

`sample_hz`はPATが軌道上の点を更新するレートです。40 kHzを整数分周できる値でなければ実行前検証で停止します。`duration_sec × sample_hz`がホログラム数になるため、値を増やすと事前計算時間とメモリ使用量も増えます。

条件固有の後処理値は、要素へ`processing_config`を追加して指定できます。

```json
"processing_config": {
  "window_us": 300,
  "hop_us": 100,
  "tracking_method": "event_weighted"
}
```

## 出力

```text
stereo_acoustools_3d_records_auto/
  auto_recording_session_<timestamp>.json
  long_random_3d_<index>_<label>_<timestamp>/
    pipeline_manifest.json
    capture_start_marker.json
    pat_camera_timing.json
    *_ideal_log.csv
    *_trajectory_preview.png
    command_trajectory.npz             # HF runのみ
    trajectory_metadata.json           # HF runのみ
    command_offset_log.csv              # HF runのみ
    command_trajectory_preview.png      # HF runのみ
    pat_start_led/
    stereo_recording/
      stereo_recording_manifest.json
      left/left_events.npz
      right/right_events.npz
      left/event_tracking/*.csv
      right/event_tracking/*.csv
      stereo_3d/stereo_3d_points.npz
    ideal_comparison_3d/
```

モニターの処理履歴は`stereo_acoustools_3d_records_auto/stereo_acoustools_3d_auto_monitor_log.csv`へ追記されます。
auto session JSONには`hardware_connection_scope=entire_auto_session`と、接続開始・終了時刻も記録されます。

## スクリプト依存関係

```text
acoustools_stereo_eventcam_3d_recording_auto.py
  ├─ stereo_acoustools_3d_auto_common.py
  │    ├─ acoustools_random3d_no_eventcam.py
  │    └─ long_random_3d_patterns_initial.json
  └─ stereo_acoustools_3d_recording_core.py
       ├─ acoustools_eventcam_sync.py
       ├─ acoustools_multitraj_no_eventcam.py
       ├─ stereo_eventcam_record_sync.py
       ├─ stereo_detect_pat_start_led.py
       └─ stereo_acoustools_3d_common.py

stereo_acoustools_3d_auto_monitor.py
  └─ stereo_acoustools_3d_postprocess_core.py
       ├─ stereo_process_recording.py
       │    ├─ eventcam_npz_track.py（左右）
       │    ├─ stereo_triangulate_tracks.py
       │    └─ stereo_plot_3d_points.py
       └─ stereo_compare_ideal_3d.py

stereo_acoustools_3d_quick_preview.py
  ├─ pipeline_manifest.json / pat_camera_timing.json
  ├─ stereo_recording_manifest.json
  ├─ left_events.npz / right_events.npz（memory-map）
  └─ NumPy + OpenCV
```

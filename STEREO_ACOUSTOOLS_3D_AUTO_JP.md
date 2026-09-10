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

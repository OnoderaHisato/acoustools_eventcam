# Plan B 追加計測（2026-08-25資料の現行計測系統合）

## 実装範囲

`plan_b_20260825/` にB1/B2/B3のplanとexportを置き、既存の
`acoustools_stereo_eventcam_3d_recording_auto.py --hf-export-dir`で記録します。
カメラserial、左Master同期、左LED ROI `600,0,1280,180`、ステレオ校正、10 kHz指令、
生イベント保存、後処理分離は既存設定を変更しません。Delay feedforwardは使用しません。

- B1: 小振幅ランダム奇数マルチサイン梯子、30 run。
- B2: zの約297 Hz線について駆動振幅依存と粒子依存を分離、15 run。
- B3: 囲い・予熱・空調条件による低周波ドリフト、6 run。

実機は必ずB1→B2→B3の順に進めます。全Plan B runでステレオpreviewとEnter確認が
強制され、`--keep-going`は禁止されます。

## ハードウェアなし検証

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\plan_b_20260825\export_B1_small_amplitude_multisine `
  --dry-run

.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\plan_b_20260825\export_B2_z_line_source `
  --dry-run

.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\plan_b_20260825\export_B3_drift_source `
  --dry-run
```

## B1: rank単位の段階計測

静止基線を先に取得し、その後rank 0から順に、1 hardware sessionにつき1 rankだけを
選びます。rankをまたぐ一括実行はPAT接続前に拒否されます。

```powershell
# 静止基線2本
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\plan_b_20260825\export_B1_small_amplitude_multisine `
  --output-dir .\stereo_acoustools_3d_records_plan_b1 `
  --label 00_static_baseline_r01 --label 00_static_baseline_r02 `
  --acknowledge-plan-b-protocol

# 最初はx rank 0のr01だけを50%で確認
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\plan_b_20260825\export_B1_small_amplitude_multisine `
  --output-dir .\stereo_acoustools_3d_records_plan_b1 `
  --hf-run 10_x_multisine_rung02.0um_r01=0.5 `
  --acknowledge-plan-b-protocol

# 保持・両眼可視の確認後、rank 0をexport scaleで取得
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\plan_b_20260825\export_B1_small_amplitude_multisine `
  --output-dir .\stereo_acoustools_3d_records_plan_b1 `
  --plan-b-rank 0 `
  --acknowledge-plan-b-protocol `
  --acknowledge-hf-retention-and-visibility
```

同じ形でrank 1、2、3、4へ進みます。`response_check.md`のHOLD runを含むrank 3/4は、
全下位rankの生存確認後だけ `--acknowledge-plan-b-hold-runs` を追加します。

## B2: 駆動条件または粒子条件を1つずつ

`--plan-b-condition driveA/driveB/driveC`はトランスデューサ振幅をそれぞれ
1.00/0.85/0.70へ自動設定し、trajectory、静止保持、manifestへ同じ値を適用します。
位相は変更しません。異なる駆動条件を1 sessionへ混在させません。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\plan_b_20260825\export_B2_z_line_source `
  --output-dir .\stereo_acoustools_3d_records_plan_b2 `
  --plan-b-condition driveA `
  --acknowledge-plan-b-protocol `
  --acknowledge-hf-retention-and-visibility
```

粒子条件は個体を交換後、同じconditionで選ばれるstaticとstaircaseの両labelを含むcontext JSONを作り、例えば次を実行します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\plan_b_20260825\export_B2_z_line_source `
  --output-dir .\stereo_acoustools_3d_records_plan_b2 `
  --plan-b-condition particle1 `
  --plan-b-context-json .\plan_b_context.json `
  --acknowledge-plan-b-protocol
```

## B3: 環境条件ごとに1 run

B3は条件変更があるため1 sessionにつき1 runだけです。context JSONには
`actual_warmup_minutes`, `shield_state`, `hvac_state`, `room_temperature_c`,
`array_surface_temperature_c`を記録します。温度計がない場合は、温度値の代わりに
`temperature_unavailable_reason`を明記します。

ここで暖機時間は外部電源を18 Vにしてからの時間ではなく、AcousTools/OpenMPDへ
非ゼロの保持ホログラムを送って粒子を保持し始めてから、カメラ収録が実際に始まるまでの
時間です。`actual_warmup_minutes`を自動タイマーの目標値として使い、実測値は別に
`pipeline_manifest.json > active_warmup.measured_to_camera_start_minutes`と
`pat_camera_timing.json > active_warmup`へ保存します。

B3ではPATを起動する前に静止ホログラムを事前計算します。60秒・10 kHzの完全に同一な
600,000フレームは1 geometryの600,000回再生へ圧縮するため、従来の約254秒の計算時間は
暖機時間へ入りません。previewとEnter確認が終わった時点で目標時間に達していなければ、
プログラムが残り時間だけ待ってからカメラを起動します。すでに目標を超えていれば待ちません。

現行のlabelと目標暖機時間は次のとおりです。

| label | 囲い | 空調 | active PAT暖機 |
|---|---|---|---:|
| `40_shield_off_warm00_static_60s` | なし | on | 0分（最短で開始） |
| `41_shield_off_warm05_static_60s` | なし | on | 5分 |
| `42_shield_off_warm10_static_60s` | なし | on | 10分 |
| `43_shield_on_warm05_static_60s` | あり | on | 5分 |
| `44_shield_on_hvac_off_warm05_static_60s` | あり | off | 5分 |
| `45_shield_off_hvac_off_warm05_static_60s` | なし | off | 5分 |

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\plan_b_20260825\export_B3_drift_source `
  --output-dir .\stereo_acoustools_3d_records_plan_b3 `
  --label 40_shield_off_warm00_static_60s `
  --plan-b-context-json .\plan_b_context.json `
  --acknowledge-plan-b-protocol
```

contextはauto session JSONと各runの`pipeline_manifest.json > automation.operator_context`
へ保存されます。駆動振幅はmanifest直下、parameters、trajectory_source、automationにも保存されます。
事前計算・静止圧縮の情報は`pipeline_manifest.json > hologram_playback`へ保存されます。

2026-08-26 19:24に取得した旧`40` runは、PATの非ゼロ出力開始からカメラ開始まで
約254秒経過しており、warm00の本計測には採用しません。成果物はpilot/タイミング診断として
削除せず保持します。新実装で`40`から再取得してください。

## 後処理

実機PCでは粒子抽出・三角測量・SINDyを実行しません。左右生イベント、ideal log、
`pat_camera_timing.json`、command artifact、manifestをrunディレクトリごとスパコンへ転送します。
解析側の固定camera-to-PAT変換とLED時刻規則は元資料
`G:\マイドライブ\Experiment\20260826\PLAN_B_MEASUREMENT_HANDOFF.md`を参照します。

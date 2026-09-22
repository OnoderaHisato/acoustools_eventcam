# vzr同定計測（x/y＋z同時正弦駆動）

作成: 2026-09-17

`G:\マイドライブ\Experiment\20260917\measurement_plan\VZR_MEASUREMENT_PLAN.md` の計測計画を、
現行のステレオ自動計測（`acoustools_stereo_eventcam_3d_recording_auto.py --hf-export-dir`）で
そのまま記録できるように整備したものです。計画書と指令生成器の写しは
`vzr_identification_20260917/REFERENCE_VZR_MEASUREMENT_PLAN.md` と
`vzr_identification_20260917/reference_make_vzr_trajectory.py` にあります。

> 2026-09-19: 計画書に「正弦波の指令は取得側の加速度上限100,000 mm/s²で弾かれる」との追記が
> ありましたが、その上限は `kind=imported`（FFハート用）のものです。本計測のplanは上限を
> 速度400 mm/s・加速度400,000 mm/s²としてあり、全指令が検査を通るので、下のコマンドは
> 今も実行できます。同じ追記で提案されたステップ型の計測は `VZR_STEP_MEASUREMENT_JP.md` に
> あります。正弦波を撮る場合は、追記のとおり10分以上休ませた直後に限ってください
> （剛性が落ちるとx 57 Hz駆動は脱出側に動きます）。

## 目的

OptiTrapモデル `F_z = az·sin(vz·Δz)·cos(vzr·d_r)` の `vzr` を実機で決めます。`vzr` は
Δz と d_r が同時に立つときにしか力に現れないため、水平（x または y）と鉛直（z）を
それぞれの共振の0.5〜0.75倍、かつ非整数比の周波数で同時に駆動します。詳細な根拠は
参照計画書を見てください。

## これまでの計測との違い

| | 既存HF／Plan B | 本計測 |
|---|---|---|
| 指令の軸 | 1 runにつき1軸 | 1 runで2軸同時（x+z または y+z） |
| 生成 `kind` | `chirp` / `multisine` / `staircase` | 新設 `multitone`（成分ごとに軸・振幅・周波数・位相） |
| ランプ | sin² 0.5 s | 同じ形（両端0.5 s） |
| 安全制限超過時 | 全体を自動縮小 | 生成時に例外で停止（黙って縮小しない） |
| 実機ack | `--acknowledge-hf-retention-and-visibility` | `--acknowledge-vzr-protocol` |
| run間確認 | 任意 | 全runでステレオpreviewとEnterを強制、`--keep-going` 禁止 |
| 遅延前置補正 | OFF | OFF（同定側で `u(t−τ)` として τ を推定） |

カメラserial、左Master同期、左LED ROI `600,0,1280,180`、校正ファイル、10 kHz更新、
記録・manifest coreは変更していません。

## 指令系列（export順）

| # | run名 | 成分 | 時間 | 計画上の位置づけ |
|---|---|---|---|---|
| 0 | `vzr_L1x_x0.4_57Hz_only` | x 0.4 mm @ 57 Hz | 4 s | x単軸の保持確認 |
| 1 | `vzr_L1z_z0.15_187Hz_only` | z 0.15 mm @ 187 Hz | 4 s | z単軸の保持確認 |
| 2 | `vzr_L1_x0.4_57Hz_z0.15_187Hz` | x 0.4 @ 57 + z 0.15 @ 187 | 8 s | L1（最弱） |
| 3 | `vzr_L2_x0.5_57Hz_z0.20_187Hz` | x 0.5 @ 57 + z 0.20 @ 187 | 8 s | L2 |
| 4 | `vzr_L3_x0.6_57Hz_z0.20_187Hz` | x 0.6 @ 57 + z 0.20 @ 187 | 8 s | **L3（推奨）** |
| 5 | `vzr_L4_x0.6_57Hz_z0.25_187Hz` | x 0.6 @ 57 + z 0.25 @ 187 | 8 s | L4 |
| 6 | `vzr_Y1y_y0.5_55Hz_only` | y 0.5 mm @ 55 Hz | 4 s | y単軸の保持確認 |
| 7 | `vzr_Y1_y0.5_55Hz_z0.20_187Hz` | y 0.5 @ 55 + z 0.20 @ 187 | 8 s | **Y1（推奨）** 軸対称の検証 |
| 8 | `vzr_Y2_y0.6_55Hz_z0.20_187Hz` | y 0.6 @ 55 + z 0.20 @ 187 | 8 s | Y2（余裕があれば） |
| 9 | `vzr_ALT_x0.7_50Hz_z0.20_187Hz` | x 0.7 @ 50 + z 0.20 @ 187 | 8 s | ALT（任意の代替、既定では記録しない） |

- 位相は参照生成器と同じく水平成分0.0 rad、z成分0.7 rad。ランプは両端0.5 s。10 kHz。
- 生成したexport 10本は、G:の参照NPZ（`positions_mm`）と全サンプルで最大3.3e-16 mmの差で
  一致します（`vzr_identification_20260917/reference_comparison_20260917.json`）。
- 安全制限はPlan Bと同じ `|v| ≤ 400 mm/s`、`|a| ≤ 400,000 mm/s²`、`|offset| ≤ 0.75 mm`。
  最大はL4の363 mm/s・352,019 mm/s²（制限の88%）、ALTの0.728 mm（97%）です。
- 模擬ではx 0.8 mm @ 57 Hzで脱出、x 0.7 @ 57 Hzは余裕が薄いため、**必ずL1から順に**上げます。
  x と y を同時に振るrunは不要です。

## 一括実行（推奨）

```powershell
# 実機を開かない確認（未生成ならexportもここで作られます）
powershell -ExecutionPolicy Bypass -File .\run_vzr_identification_all.ps1 -DryRunOnly

# 実機計測（L1x → L1z → L1 → L2 → L3 → L4 → Y1y → Y1 → Y2 の9 run）
powershell -ExecutionPolicy Bypass -File .\run_vzr_identification_all.ps1
```

動作:

- exportは `vzr_identification_20260917\export_vzr_identification` に生成し、
  `export_manifest.json` があれば再生成しません（作り直す場合は `-Regenerate`）。
- PAT/OpenMPDの接続は1回で、粒子は中心で保持されたまま次のrunへ進みます。
- **全runの前にステレオpreviewとEnter確認があります。** 前のrunで粒子が落ちていないこと、
  中心で安定していること、左右両眼に見えていることを確認してEnterを押してください。
  落ちていたらそこでCtrl+Cを押します。sessionは `interrupted` で止まり、PAT出力は停止します。
- 粒子の脱落は自動では検知しません。
- 出力先は既定で `.\stereo_acoustools_3d_records_vzr`（`-OutputDir` で変更）。
- 一部だけ記録する場合は `-Labels`（順序は書き順に関係なくexport順）。ALTを含める場合は
  `-IncludeAlt`。全指令を一律に縮小して試す場合は `-CommandScale 0.5` など。

```powershell
# y+z系列だけ
powershell -ExecutionPolicy Bypass -File .\run_vzr_identification_all.ps1 `
  -Labels "vzr_Y1y_y0.5_55Hz_only,vzr_Y1_y0.5_55Hz_z0.20_187Hz,vzr_Y2_y0.6_55Hz_z0.20_187Hz"

# L3を1本だけ（保持確認を飛ばすので、直前のrunで粒子生存を確認済みのときだけ）
powershell -ExecutionPolicy Bypass -File .\run_vzr_identification_all.ps1 `
  -Labels "vzr_L3_x0.6_57Hz_z0.20_187Hz"
```

## スクリプトを使わない場合

```powershell
# 生成（実機を開きません。command_offset_log.csv も必ず出力されます）
.\venv\Scripts\python.exe .\hf_identification_trajectory.py `
  --plan .\vzr_identification_20260917\vzr_identification_plan.json `
  --output-dir .\vzr_identification_20260917\export_vzr_identification

# ハードウェアを開かない確認
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\vzr_identification_20260917\export_vzr_identification `
  --dry-run

.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\vzr_identification_20260917\export_vzr_identification `
  --preview-only

# 実機計測（export順に全10 run。ALTも含まれるので、不要なら --label で絞る）
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\vzr_identification_20260917\export_vzr_identification `
  --output-dir .\stereo_acoustools_3d_records_vzr `
  --acknowledge-vzr-protocol
```

`--label` は複数指定でき、記録はexport順です。`--hf-command-scale`（0〜1）は読み込み後に
全指令へ掛かり、制限は再検査されます。`--hf-run LABEL=SCALE` も従来どおり使えます。
`--acknowledge-hf-retention-and-visibility` は本計測では不要です（vzrのrunは
`--acknowledge-vzr-protocol` と全runの強制checkpointで代替）。
`--unattended-after-first-checkpoint`（step-response専用）と `--keep-going` は使えません。
vzr exportと他のexportを同じsessionに混在させることもできません。

## 安全ゲート

- 生成時: `kind=multitone` は制限（offset／速度／加速度）を超えると例外で止まり、
  自動縮小しません。`safety_scale_applied` は常に1.0で、`safety_limit_fractions` に
  制限に対する比率が残ります。
- 読み込み時: `trajectory_metadata.json` の `axes` と `generation_detail.components` を要求し、
  `measurement_family=vzr_identification` で `multitone`／`static` 以外のkindは拒否します。
  読み込み後のscale適用値についても従来どおり制限を再検査します。
- 実機時: `--acknowledge-vzr-protocol` がなければPAT接続前に終了コード2で停止します。
  全runでpreviewとEnterが強制され、`--keep-going` は拒否されます。

## 出力

各runは他のHF計測と同じ形式で、`stereo_acoustools_3d_records_vzr\vzr_identification_<index>_<run名>_scale100_<時刻>\`
に `pipeline_manifest.json`（`processing_status=pending`）、`*_ideal_log.csv`、
`command_trajectory.npz`／`trajectory_metadata.json`／`command_offset_log.csv`（SHA-256付き）、
`pat_start_led\`、`stereo_recording\left|right\*_events.npz` が保存されます。
`pipeline_manifest.json` の `trajectory_source.reference` に計画表の段（`stage`）、
物理Δの期待値、SE(vzr)の見込みが入ります。session JSONの `trajectory_mode` は
`vzr_identification` です。

記録区間は軌道8 s（保持確認は4 s）＋tail margin 2.0 s（既定）で、PAT送信開始遅延
（80,000フレームで約1 s）を含めても軌道末尾は記録内に入ります。

## 記録後

- 粒子抽出・3D三角測量は従来どおり `stereo_acoustools_3d_postprocess.py <RUN>` または
  スパコンで行います（このPCでは実行しません）。
- vzrの同定（`real_vzr_fit.py`、`sindy_dataset_3d/state_control.npz`）は解析側の作業で、
  このリポジトリには含まれていません。計画書の「解析手順」を参照してください。
- 位置合わせはステップ計測と同じ `spatial_alignment = fixed`（session基準のcamera→PAT変換）で
  比較を作ります。記録時に `--camera-to-pat-transform` を指定した場合は、その変換のSHA-256が
  manifestに残ります。
- 解析では記録8 sの最初0.5 s（ランプ）と末尾を除きます。
- 合否は `vzr_valid`（`R²_z ≥ 0.5` かつ `max|Δz| ≥ 0.1 mm`）と、L2〜L4のrun間でvzrが
  SEの2倍以内に一致することです。x+zとy+zのvzrが10%超ずれる場合は方向依存があり、
  軸対称近似の拡張が必要です。

## テスト

```powershell
.\venv\Scripts\python.exe -B -m unittest test_vzr_identification -v
```

`multitone` 生成が参照生成器の式と1e-12 mm以内で一致すること、制限超過で例外になること、
公式planの順序・時間・制限、exportの読み込みと2軸の位置生成、dry-runで実機を開かないこと、
ackなし／`--keep-going`／無人モード／他exportとの混在の拒否、全runの強制checkpointを確認します。

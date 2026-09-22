# ステップ型vzr計測（水平1軸とzの同時ジャンプ）

作成: 2026-09-19

`G:\マイドライブ\Experiment\20260918\measurement_plan\vzr_step\VZR_STEP_PLAN.md` の計測計画を、
現行のステレオ自動計測（`acoustools_stereo_eventcam_3d_recording_auto.py --hf-export-dir`）で
そのまま記録できるように整備したものです。計画書と指令生成器の写しは
`vzr_step_20260919/REFERENCE_VZR_STEP_PLAN.md`、
`vzr_step_20260919/reference_make_vzr_step_trajectory.py`、
2026-09-19に追記された親計画書の写しは
`vzr_step_20260919/REFERENCE_VZR_MEASUREMENT_PLAN_20260919.md` にあります。

## 一括実行

```powershell
# 実機を開かない確認（未生成ならexportもここで作られます）
powershell -ExecutionPolicy Bypass -File .\run_vzr_step_all.ps1 -DryRunOnly
```

```powershell
# 実機計測（XS1 → XS2 → XS2 → XS2 の4 run。最初のrunだけpreviewとEnter、以後は自動）
powershell -ExecutionPolicy Bypass -File .\run_vzr_step_all.ps1
```

計画書の手順は次のとおりです。スクリプトは休止時間を計りません。

1. トラップを10分以上休ませる。
2. `XS1` を1本。粒子が保持され、ジャンプごとにxとzが振れて0.3 s以内に静まるのを目で確認する。
3. `XS2` を2〜3本（間隔は一定）。
4. 余裕があれば `XS3` を1本、`YS2` を1本。

| オプション | 既定 | 説明 |
|---|---|---|
| `-Runs "XS1,XS2,XS2,XS2"` | 左記 | runの並び（XS1／XS2／XS3／YS1／YS2／YS3。繰り返し可） |
| `-CommandScale 1.0` | 1.0 | 選んだ全runの指令に掛ける倍率（0より大きく1以下） |
| `-ConfirmEachRun` | なし | 全runでpreviewとEnter（2026-09-20より前の動作） |
| `-Unattended` | なし | 失敗時も尋ねず、自動で撮り直す |
| `-AutomaticCaptureRetries 2` | 2 | 無人時の自動撮り直し回数 |
| `-CaptureTailMarginSec 5` | 5 | 記録末尾の余裕。最後の保持0.5 sまで記録に入れるため |
| `-KeepFailedCaptures` | なし | 失敗した試行のファイルを残す（診断用） |
| `-Regenerate` | なし | exportを作り直す（planが変わったときは指定しなくても自動で作り直します） |
| `-OutputDir` | `.\stereo_acoustools_3d_records_vzr_step` | 出力先 |
| `-DryRunOnly` | なし | 実機を開かない |

例:

```powershell
# まず保持確認の1本だけ
powershell -ExecutionPolicy Bypass -File .\run_vzr_step_all.ps1 -Runs "XS1"
```

```powershell
# 本計測の後で、余裕があれば
powershell -ExecutionPolicy Bypass -File .\run_vzr_step_all.ps1 -Runs "XS3,YS2"
```

間隔を一定にしたい場合は `-Unattended` が向いています（run間は操作者の入力を待たず、
1本あたりの所要時間がほぼ一定になります）。ただし粒子の脱落は自動検知されません。

## 指令系列（export順）

1ブロックは10ジャンプで、3ブロック繰り返します。31保持・30ジャンプ（水平単独6、z単独6、同時18）、
保持0.3 s（最初と最後は0.5 s）、9.7 s、10 kHz、97,000点。水平を h として (h, z) の順に

```text
(0,0) →(+,0) →(+,+) →(0,0) →(−,−) →(−,0) →(0,0) →(+,−) →(0,0) →(−,+) →(0,0)
```

| index | run名 | 水平 | z | 1サンプルの最大変化 | 脱出境界2.144 mm比 | tier | 用途 |
|---|---|---|---|---|---|---|---|
| 000 | `vzrstep_XS1` | x ±0.6 mm | ±0.4 mm | 0.721 mm | 33.6% | C | 最初の1本（保持確認を兼ねる） |
| 001 | `vzrstep_XS2` | x ±0.8 | ±0.5 | 0.943 | 44.0% | C | 本計測。2〜3本 |
| 002 | `vzrstep_XS3` | x ±1.0 | ±0.6 | 1.166 | 54.4% | D | 余裕があれば |
| 003 | `vzrstep_YS1` | y ±0.6 | ±0.4 | 0.721 | 33.6% | C | y+zの保持確認（任意） |
| 004 | `vzrstep_YS2` | y ±0.8 | ±0.5 | 0.943 | 44.0% | C | 軸対称の検証。1本 |
| 005 | `vzrstep_YS3` | y ±1.0 | ±0.6 | 1.166 | 54.4% | D | 任意 |

- 6本とも、解析側の `commands/vzrstep_*.npz` の `positions_mm` と全サンプルでビット一致します
  （`vzr_step_20260919/reference_comparison_20260919.json`）。
- tierは表示用の区分です（S1／S2は記録実績のある1.05 mm以下なのでC、S3は1.166 mmなのでD）。
  解析側の試作exportは全runをCとしていましたが、実行時の扱いに差はありません。
- 1 runで使うホログラムは位置7種類だけなので、準備は短時間です。

## 生成の仕組み

`hf_identification_trajectory.py` の `kind=staircase` に、保持位置を `[x, y, z]` で並べる
`level_sequence_mm` を追加しました。1サンプルで複数の軸が同時に変わる指令を作れます。

```json
{
  "name": "vzrstep_XS2",
  "kind": "staircase",
  "tier": "C",
  "hold_sec": 0.3,
  "initial_hold_sec": 0.5,
  "final_hold_sec": 0.5,
  "level_sequence_mm": [[0.8, 0.0, 0.0], [0.8, 0.0, 0.5], [0.0, 0.0, 0.0], "..."],
  "repeats_within_run": 3
}
```

- `level_sequence_mm` は最初の中心保持の後に続く1ブロック分です。ブロックは中心 `[0, 0, 0]` で
  終わる必要があり、同じ位置を続けて置くことはできません。`amplitudes_mm`／`directions`／
  `order_seed` とは併用できません。`axis` は動かした軸から自動で決まります（`xz`、`yz`）。
- 安全検査は従来のstaircaseと同じで、振幅だけを見ます: 中心からの距離、1サンプルの変化、
  推定脱出境界2.144 mm。いずれも**ベクトルの大きさ**で判定するので、同時ジャンプは
  √(S_h² + S_z²) で評価されます。速度・加速度の制限は適用されません（意図した不連続指令のため）。
- 各runの `safety_limits` は設計したジャンプのすぐ上（0.73／0.95／1.17 mm）に固定してあり、
  生成の誤りでそれより大きいジャンプができた場合は例外で止まります。自動縮小はしません。
- 従来の単軸staircase（`axis` と `amplitudes_mm`）の生成結果は変わっていません。

カメラserial、左Master同期、左LED ROI `600,0,1280,180`、校正ファイル、10 kHz更新、
記録・manifest coreは変更していません。

## スクリプトを使わない場合

```powershell
# 生成（実機を開きません）
.\venv\Scripts\python.exe .\hf_identification_trajectory.py `
  --plan .\vzr_step_20260919\vzr_step_plan.json `
  --output-dir .\vzr_step_20260919\export_vzr_step
```

```powershell
# ハードウェアを開かない確認
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\vzr_step_20260919\export_vzr_step `
  --output-dir .\stereo_acoustools_3d_records_vzr_step `
  --hf-run vzrstep_XS1=1 --hf-run vzrstep_XS2=1 --hf-run vzrstep_XS2=1 --hf-run vzrstep_XS2=1 `
  --dry-run
```

```powershell
# 実機計測
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\vzr_step_20260919\export_vzr_step `
  --output-dir .\stereo_acoustools_3d_records_vzr_step `
  --hf-run vzrstep_XS1=1 --hf-run vzrstep_XS2=1 --hf-run vzrstep_XS2=1 --hf-run vzrstep_XS2=1 `
  --capture-tail-margin-sec 5 `
  --acknowledge-step-response-risk
```

## 安全ゲート

ステップ応答と同じ扱いです。

- 実機には `--acknowledge-step-response-risk` が必要です。ない場合はPAT接続前に終了コード2で止まります。
- 既定では最初のrunだけステレオpreviewとEnter確認を行い、残りは自動で始まります（2026-09-20に変更）。
  記録やLED検出に失敗したときは「同じ軌道を再計測しますか？ (Y/n)」を出します。全runで確認するなら `-ConfirmEachRun`。
- `--keep-going` は使えません。失敗・拒否・Ctrl+Cでsessionは止まり、PATを停止します。

## 出力

```text
stereo_acoustools_3d_records_vzr_step/
  auto_recording_session_<timestamp>.json
  step_response_identification_001_vzrstep_XS2_scale100_<timestamp>/
    pipeline_manifest.json (processing_status=pending)
    *_ideal_log.csv
    command_trajectory.npz / trajectory_metadata.json / command_offset_log.csv
    stereo_recording/left|right/*_events.npz
```

既定の出力先では、フォルダ名は短縮されません（run名がそのまま入ります）。

## 記録後

後処理はFFハートと同じです（3D化 → `stereo_compare_ideal_3d.py --spatial-alignment fixed`）。
同定は解析側の `vzr_step_fit.py` が `ideal_comparison_3d/stereo_ideal_comparison.npz` を読みます。
重い粒子抽出はこのPCでは実行しません。

## 解析側の試作exportについて

解析側の `vzr_step/export_vzr_step`（`export_vzr_step_for_acquisition.py` の出力）は、取得PCの
現行ローダのdry-runをそのまま通ります（2軸同時の変化も `axis: "xz"` も受理。2026-09-19に確認）。
計画書にある `--stagger-ms 1` や `axis` を `"x"` にする回避策は不要です。取得PCでは、planから
再現でき、planとの一致を検査できる `vzr_step_20260919/export_vzr_step` を使ってください。
指令の数値は同一です。

## 正弦波のvzr計測との関係

親計画書の2026-09-19追記は「正弦波の指令は取得側の加速度上限100,000 mm/s²で弾かれる」としていますが、
これは `kind=imported`（FFハート用）の上限です。2026-09-17に整備した正弦波のvzr計測
（`VZR_IDENTIFICATION_MEASUREMENT_JP.md`、`kind=multitone`）は、planの上限を速度400 mm/s・
加速度400,000 mm/s²としてあり、L1〜L4・Y1・Y2・ALTの全指令が検査を通ります
（追記の選択肢(A)に相当）。したがって `run_vzr_identification_all.ps1` は今も実行できます。
どちらを撮るかは計画側の判断です。追記にあるとおり、正弦波のx 57 Hz駆動は剛性が落ちると
脱出側に動くので、撮る場合は10分以上休ませた直後に限ってください。

## テスト

```powershell
.\venv\Scripts\python.exe -B -m unittest test_vzr_step_plan -v
```

13件: 解析側生成器とのビット一致、保持配置と繰り返し、同時ジャンプのベクトル判定、脱出境界、
不正な指定の拒否、単軸staircaseの不変、export・preview・実機読み込み、フォルダ名、dry-run、
ack不足と `--keep-going` の拒否、繰り返しlabelの順序と全runのcheckpoint、無人モード。

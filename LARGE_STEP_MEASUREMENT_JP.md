# 大振幅1軸ステップ計測（水平の力の上限）

作成: 2026-09-20

`G:\マイドライブ\Experiment\20260919\measurement_plan\NEXT_MEASUREMENTS_20260920.md` の3番と
`large_step/make_large_step_trajectory.py` を、現行のステレオ自動計測
（`acoustools_stereo_eventcam_3d_recording_auto.py --hf-export-dir`）でそのまま記録できるように
整備したものです。計画書と生成器の写しは `large_step_20260920/` にあります。

## 目的

1 mm以下のステップでは `ar` と `vxr` は積 `k_x = ar·vxr` しか決まりません。1.5 mm以上にすると
飽和長 `vxr` が分離でき、水平の力の上限 `A_r = k_x / vxr` が出ます。

## 一括実行

```powershell
# 実機を開かない確認（未生成ならexportもここで作られます）
powershell -ExecutionPolicy Bypass -File .\run_large_step_all.ps1 -DryRunOnly
```

```powershell
# 実機計測（既定は XL12 → XL15 の2 run）
powershell -ExecutionPolicy Bypass -File .\run_large_step_all.ps1
```

**必ず小さい段から上げてください。** XL12 → XL15 で粒子の戻り方が鈍くなったら、そこで止めます
（それ自体が答えになります）。余裕があれば XL18、XL20、y系列へ進みます。

```powershell
powershell -ExecutionPolicy Bypass -File .\run_large_step_all.ps1 -Runs "XL18"
```

| オプション | 既定 | 説明 |
|---|---|---|
| `-Runs "XL12,XL15"` | 左記 | runの並び（XL12／XL15／XL18／XL20／YL12〜YL20。繰り返し可） |
| `-ConfirmEachRun` | なし | 全runでpreviewとEnter（2026-09-20より前の動作） |
| `-Unattended` | なし | 失敗時に尋ねず自動で撮り直す（`-AutomaticCaptureRetries` 既定2回） |
| `-CommandScale 1.0` | 1.0 | 指令に掛ける倍率（0より大きく1以下） |
| `-CaptureTailMarginSec 5` | 5 | 記録末尾の余裕 |
| `-KeepFailedCaptures` | なし | 失敗した試行のファイルを残す |
| `-Regenerate` | なし | exportを作り直す（planが変わると自動で作り直します） |
| `-OutputDir` | `.\stereo_acoustools_3d_records_large_step` | 出力先 |
| `-DryRunOnly` | なし | 実機を開かない |

既定では**最初のrunだけ**previewとEnter確認があり、残りは自動で続きます。記録やLED検出に
失敗したときは「同じ軌道を再計測しますか？ (Y/n)」を出します。

## 指令系列（export順）

1軸だけを `0 → +S → 0 → −S → 0` と動かし、3周で12ジャンプです。保持0.4 s（両端0.5 s）、
13保持、5.4 s、10 kHz、54,000点。`+S → −S` の2Sジャンプはしません。

| index | run名 | 軸 | 1ジャンプ | 脱出境界2.144 mm比 | tier | 用途 |
|---|---|---|---|---|---|---|
| 000 | `largestep_XL12` | x | 1.2 mm | 56% | D | 最初の段 |
| 001 | `largestep_XL15` | x | 1.5 mm | 70% | E | ここから飽和長が分離する |
| 002 | `largestep_XL18` | x | 1.8 mm | 84% | F | XL15が健全なときだけ |
| 003 | `largestep_XL20` | x | 2.0 mm | 93% | G | 2 mmのxステップは8月に保持実績あり |
| 004--007 | `largestep_YL12`〜`YL20` | y | 同上 | 同上 | 同上 | y方向の確認 |

- 8本とも、解析側の `large_step/commands/vzrstep_*L*.npz` とビット一致します
  （`large_step_20260920/reference_comparison_20260920.json`）。
- 各runの `safety_limits` は設計したジャンプのすぐ上に固定してあります（生成の誤りは例外で止まります）。
- 1 runで使うホログラムは位置3種類（中心・±S）だけです。

## 安全について

- 実機には `--acknowledge-step-response-risk` が必要です（スクリプトが付けます）。
- 1.8／2.0 mmは推定力最大点（約1.07 mm）より外から戻る動きになります。模擬では全段で保持しますが、
  実機では余裕が薄いので、previewで戻り方を見ながら1段ずつ上げてください。
- `--keep-going` は使えません。失敗・拒否・Ctrl+Cでsessionは止まり、PATを停止します。

## 出力

```text
stereo_acoustools_3d_records_large_step/
  auto_recording_session_<timestamp>.json
  step_response_identification_000_largestep_XL12_scale100_<timestamp>/
    pipeline_manifest.json (processing_status=pending)
    ...
```

解析は解析側の `vzr_step/vzr_step_fit.py`（水平の段だけ当てる）です。

## テスト

```powershell
.\venv\Scripts\python.exe -B -m unittest test_large_step_plan -v
```

8件: 解析側生成器とのビット一致、段・tier・保持数、制限超過の拒否、フォルダ名、実機読み込み、
dry-runとack不足の拒否、既定の確認動作（最初の1本だけpreviewとEnter、失敗時はY/n）、
自動撮り直しとの排他。

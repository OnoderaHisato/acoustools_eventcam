# ステレオ計測スクリプト・実行ワークフロー

更新日: 2026-07-28

対象: 左イベントカメラ `00000508`、右イベントカメラ `00000509`、Ossilaリニアステージ、PAT/AcousTools

この文書は、現在の推奨フローである

1. 左右の単眼校正
2. 同期ステレオ校正
3. 同期イベント記録と左カメラ座標での3D復元
4. 左カメラ座標からPAT座標への登録・変換
5. Ossilaステージによる距離精度実験

について、使用するスクリプト、入出力、ファイル同士の対応、実行順、PowerShellでのコマンド例をまとめたものである。コマンドは原則として、このファイルがある `eventcam_control` ディレクトリで実行する。

## 1. 最重要事項

### 1.1 チェッカーボードの1マスは必ず7.12 mm

使用画像は [checkerboard_10x7_normal.png](./checkerboard_10x7_normal.png) である。外側が10×7マス、OpenCVが検出する内点は9×6点である。

**`--square-mm 7.1` は誤りであり、必ず `--square-mm 7.12` とする。**

単眼・ステレオ校正では、入力ミスを止めるため `--require-square-mm 7.12` も併用する。点滅GIFは使わない。液晶のリフレッシュによってイベントが生じるため、静止した通常画像を細かく動かす必要もない。

### 1.2 現在の最終校正とPAT変換

| 項目 | 現在採用するファイル・値 |
|---|---|
| 左単眼校正 | `checkerboard_calib_left_on_stage_20260727/single_calibration_square7p12_left00508.npz` |
| 左単眼RMS | 0.260197 px |
| 右単眼校正 | `checkerboard_calib_right_on_stage_20260728/single_calibration_square7p12_right00509.npz` |
| 右単眼RMS | 0.241580 px |
| 最終ステレオ校正 | `stereo_checkerboard_calib_on_stage_20260728/stereo_calibration_square7p12_final.npz` |
| ステレオRMS | 0.219852 px |
| 採用ペア | 39組。`pose_020` は除外 |
| ベースライン長 | 120.138247 mm |
| 校正SHA-256 | `11cbf17f2055da8ccc6fa438a03a9fa164a6edafdc29f36aca9ed4c8623be416` |
| PAT変換 | `pat_stereo_grid_records/pat_camera_registration_grid_27_20260728_144326/registration/camera_to_pat_transform.npz` |
| PAT登録条件 | Ossila hardware readback = 200.0 mm |
| PAT登録品質 | 27/27 inlier、fit RMS 0.267499 mm、validation RMS 0.309973 mm、quality PASS |

PAT変換は上記の最終ステレオ校正と同じSHA-256を記録している。別のステレオ校正で作った3Dデータには適用しない。

### 1.3 カメラやPATを物理的に動かしたら再校正・再登録する

- レンズ、左右カメラの相対位置、フォーカスを変更した場合は、左右単眼校正からやり直す。
- 左右カメラの相対位置を変えた場合は、少なくともステレオ校正をやり直す。
- ステレオカメラ全体とPATの相対位置を変えた場合は、カメラ→PAT登録をやり直す。
- 現在のカメラ→PAT変換は、登録時のステージ位置 `hardware = 200.0 mm` における変換である。

単眼・ステレオ校正を `hardware = 200 mm` で行うことは、現在の取付状態と最近点で光学系を確定するという意味で自然である。ただし、ステージ位置そのものは単眼・ステレオ校正の数式への入力ではない。校正のmetric scaleは7.12 mmのマス寸法から得る。200 mmのreadbackが座標変換に直接必要になるのは、カメラ→PAT登録と、その登録をステージ移動後の姿勢へ展開するときである。

## 2. 座標系と変換の関係

### 2.1 左カメラ座標

OpenCVの左カメラ座標は次の向きで、単位はmmである。

- X: 画像の右方向
- Y: 画像の下方向
- Z: カメラ前方

通常のステレオ3D復元結果 `points_left_cam_mm` はこの座標系で保存される。

### 2.2 右カメラとの関係

ステレオ校正NPZ内の `R`, `T` は、左カメラ座標の点を右カメラ座標へ写す。

```text
p_right = R @ p_left + T
```

左右画像中の同じ粒子の2D位置と、左右の内部パラメータ、`R`, `T` を使って三角測量すると、左カメラ座標の3D点が得られる。

### 2.3 PAT座標

現在はAcousToolsの目標座標とPAT座標を同じものとして扱う。

```text
AcousTools [0, 0, 0] mm = PAT [0, 0, 0] mm
```

物理的にPAT中心やカメラを120 mmの高さに設置していても、ソフトウェア上で一律に `[0, 0, 120]` を加える必要はない。PATの原点・軸はAcousToolsへ与える座標そのもので定義される。

左カメラ座標からPAT座標への剛体変換は次式である。

```text
p_pat_mm = R_camera_to_pat @ p_left_camera_mm + t_camera_to_pat_mm
```

### 2.4 Ossilaステージ座標

[Ossila_LinearStage_Python_sample/config.md](./Ossila_LinearStage_Python_sample/config.md) の各行は次の形式である。

```text
<AXIS> <USB_SERIAL> <DIRECTION>
```

現在のY軸設定は次のとおりである。

```text
Y E662608797387B2E +1
```

- `Y`: この実験で使う論理軸名
- `E662608797387B2E`: Windows/USB側でステージを識別するシリアル
- `+1`: hardware座標とglobal座標の向きを反転しない
- コントローラ内部の `<serial?>` 応答は別の値で、現在のprobeでは `5`

ステージ距離実験では `hardware = 200 mm` を最近点・登録点とし、`hardware = 5 mm` までカメラを遠ざける。設定が `datum_mm = 200`、`direction = +1` のとき、

```text
global_mm = hardware_mm - 200
```

したがって、`hardware = 200, 180, ..., 5 mm` は `global = 0, -20, ..., -195 mm` に対応する。現在の実験設定では、このglobal変位をPAT座標のカメラ移動 `[0, +1, 0]` mm/mmとして解釈する。

## 3. 全体の依存関係

```text
左チェッカーボード画像 ──> 左単眼校正NPZ ─┐
                                             ├─> ステレオ校正NPZ
右チェッカーボード画像 ──> 右単眼校正NPZ ─┘          │
                                                        │
左右同期イベントNPZ ─> 左右2D tracking CSV ────────────┤
                                                        v
                                         左カメラ座標3D NPZ
                                                        │
PAT既知27点 ─> PAT grid同期記録 ─> カメラ→PAT変換NPZ ──┤
                                                        v
                                                PAT座標3D NPZ

ステレオ校正NPZ + カメラ→PAT変換JSON + Ossila readback
                         └─> ステージ距離精度のPAT座標解析
```

ステレオ校正NPZが、通常計測、PAT登録、ステージ実験の共通基準である。途中で校正ファイルを混在させない。

### 3.1 成果物の受け渡し

| 前工程の出力 | 次工程での入力先 |
|---|---|
| 左 `calib_images/` | `single_camera_calibrate.py --images` |
| 右 `calib_images/` | `single_camera_calibrate.py --images` |
| 左単眼校正NPZ | `stereo_camera_calibrate.py --left-intrinsics` |
| 右単眼校正NPZ | `stereo_camera_calibrate.py --right-intrinsics` |
| 左右同期 `left/right/calib_images/` | `stereo_camera_calibrate.py --left-images/--right-images` |
| ステレオ校正NPZ | `stereo_process_recording.py --stereo-calibration` |
| 同じステレオ校正NPZ | `pat_stereo_grid_config.json` の `camera.stereo_calibration` |
| 通常記録のrun directory | `stereo_process_recording.py` の位置引数 |
| `stereo_3d/stereo_3d_points.npz` | `stereo_apply_pat_transform.py` の位置引数 |
| `camera_to_pat_transform.npz` | `stereo_apply_pat_transform.py --transform` |
| `camera_to_pat_transform.json` | `stereo_stage_accuracy_config.json` の `analysis.camera_to_pat_transform` |
| stage accuracy session | `stereo_stage_accuracy_analyze.py` の位置引数 |

## 4. スクリプト一覧

| スクリプト | 役割 | 主入力 | 主出力 |
|---|---|---|---|
| [eventcam_checkerboard_calibration_capture.py](./eventcam_checkerboard_calibration_capture.py) | 単眼用チェッカーボード取得 | カメラ、通常チェッカーボード | `calib_images/*.png`、RAW、manifest |
| [single_camera_calibrate.py](./single_camera_calibrate.py) | 単眼内部パラメータ推定 | 1台分の `calib_images`、7.12 mm | 単眼校正NPZ、ビュー別CSV、debug画像 |
| [stereo_checkerboard_sync_capture.py](./stereo_checkerboard_sync_capture.py) | 左右同期チェッカーボード取得 | 2台のカメラ、通常チェッカーボード | 左右同名の `calib_images/pose_###.png` |
| [stereo_camera_calibrate.py](./stereo_camera_calibrate.py) | 左右カメラの相対姿勢推定 | 左右単眼NPZ、左右画像ペア | ステレオ校正NPZ、ペア別CSV、debug画像 |
| [stereo_eventcam_record_sync.py](./stereo_eventcam_record_sync.py) | 通常の左右同期イベント記録 | 左右カメラ | 左右イベントNPZ、同期manifest |
| [eventcam_npz_track.py](./eventcam_npz_track.py) | 1台分の粒子2D追跡 | イベントNPZ | raw/interp tracking CSV |
| [stereo_process_recording.py](./stereo_process_recording.py) | 通常計測の一括後処理 | 1記録run、ステレオ校正NPZ | 左右tracking、左カメラ座標3D |
| [stereo_triangulate_tracks.py](./stereo_triangulate_tracks.py) | 左右2D軌跡の三角測量 | 左右interp CSV、ステレオ校正NPZ | `stereo_3d_points.npz/csv` |
| [stereo_apply_pat_transform.py](./stereo_apply_pat_transform.py) | 左カメラ座標をPAT座標へ変換 | 3D NPZ、PAT変換NPZ | PAT座標NPZ、CSV、summary |
| [stereo_plot_3d_points.py](./stereo_plot_3d_points.py) | 3D結果の静止画作成 | 3D NPZ | 3D/投影PNG |
| [stereo_3d_viewer.py](./stereo_3d_viewer.py) | 3D結果の対話表示 | 3D NPZまたは対応点CSV | ローカルWeb 3D viewer |
| [pat_stereo_grid_capture.py](./pat_stereo_grid_capture.py) | AcousToolsで既知27点へ動かして同期記録 | PAT、2台のカメラ、grid config | PAT grid session |
| [pat_stereo_grid_process.py](./pat_stereo_grid_process.py) | 27点からカメラ→PAT剛体変換を推定 | PAT grid session | 変換NPZ/JSON、対応点CSV、評価 |
| [stereo_stage_accuracy_capture.py](./stereo_stage_accuracy_capture.py) | Ossila距離精度実験の計画・取得 | stage config、PAT、カメラ | immutable session、全計測run |
| [stereo_stage_accuracy_analyze.py](./stereo_stage_accuracy_analyze.py) | 距離・立方体精度をPAT座標で集計 | stage session | CSV、JSON、PNG、MDレポート |
| [stereo_stage_accuracy_common.py](./stereo_stage_accuracy_common.py) | stage実験の共通設定・座標計算 | 上記スクリプト内部から使用 | 直接実行しない |

`eventcam_npz_track.py` と `stereo_triangulate_tracks.py` は個別実行もできるが、通常は `stereo_process_recording.py` から呼び出す。

[stereo_checkerboard_calibration_capture.py](./stereo_checkerboard_calibration_capture.py) は旧方式のステレオ取得である。現在はハードウェア同期、共通時間窓、左右同時採否を行う `stereo_checkerboard_sync_capture.py` を使う。通常記録も旧 [stereo_eventcam_record.py](./stereo_eventcam_record.py) より `stereo_eventcam_record_sync.py` を優先する。

## 5. 推奨実行順

| 順番 | 実行内容 | いつ必要か |
|---:|---|---|
| 1 | 左単眼画像取得・単眼校正 | 光学系を変更したとき |
| 2 | 右単眼画像取得・単眼校正 | 光学系を変更したとき |
| 3 | 同期ステレオ画像取得・ステレオ校正 | 左右カメラの相対配置を変更したとき |
| 4 | PAT 27点取得・カメラ→PAT登録 | カメラ全体またはPATを動かしたとき |
| 5 | 通常の同期イベント記録 | 計測ごと |
| 6 | 2D追跡・三角測量 | 記録ごと |
| 7 | PAT座標変換 | PAT座標が必要な記録ごと |
| 8 | ステージ距離精度取得・解析 | 距離精度実験を行うとき |

現在の校正とPAT登録をそのまま使う通常計測は、手順5から開始できる。

## 6. 手順1・2: 左右の単眼校正

### 6.1 単眼画像を取得する

将来取り直す場合は、既存結果へ混在させず、新しい空の出力フォルダを使う。次はフォルダ名の例である。

左カメラ:

```powershell
.\venv\Scripts\python.exe eventcam_checkerboard_calibration_capture.py `
  --serial 00508 `
  --output-dir checkerboard_calib_left `
  --checkerboard-image checkerboard_10x7_normal.png `
  --square-mm 7.12 `
  --require-square-mm 7.12 `
  --duration-sec 0.2 `
  --frame-window-us 20000 `
  --frame-window-us-list 10000,20000,30000,40000 `
  --candidate-count 1 `
  --render-mode off
```

右カメラ:

```powershell
.\venv\Scripts\python.exe eventcam_checkerboard_calibration_capture.py `
  --serial 00509 `
  --output-dir checkerboard_calib_right `
  --checkerboard-image checkerboard_10x7_normal.png `
  --square-mm 7.12 `
  --require-square-mm 7.12 `
  --duration-sec 0.2 `
  --frame-window-us 20000 `
  --frame-window-us-list 10000,20000,30000,40000 `
  --candidate-count 1 `
  --render-mode off
```

`--duration-sec 0.2` 自体はクリティカルな問題ではない。0.2秒の中に十分なイベントがあり、9×6内点が安定して検出できることが採用条件である。処理中は既定で新着イベントをdrainして捨てるため、古いイベントを後からプレビューへ描き切る動作を抑える。`--preview-during-processing` は通常付けない。

主な出力:

```text
checkerboard_calib_left/
├─ calib_images/       校正へ入力する採用画像
├─ corner_debug/       検出内点を重ねた確認画像
├─ candidates/         候補画像
├─ raw/                元のRAWと各poseの情報
└─ capture_manifest.json
```

### 6.2 単眼内部パラメータを計算する

左:

```powershell
.\venv\Scripts\python.exe single_camera_calibrate.py `
  --images checkerboard_calib_left\calib_images `
  --camera-serial 00000508 `
  --square-mm 7.12 `
  --require-square-mm 7.12 `
  --output checkerboard_calib_left\single_calibration_square7p12_left00508.npz `
  --debug-dir checkerboard_calib_left\calibration_debug_square7p12
```

右:

```powershell
.\venv\Scripts\python.exe single_camera_calibrate.py `
  --images checkerboard_calib_right\calib_images `
  --camera-serial 00000509 `
  --square-mm 7.12 `
  --require-square-mm 7.12 `
  --output checkerboard_calib_right\single_calibration_square7p12_right00509.npz `
  --debug-dir checkerboard_calib_right\calibration_debug_square7p12
```

単眼NPZの主要パラメータ:

| キー | 意味 |
|---|---|
| `camera_matrix` | 焦点距離 `fx, fy` と主点 `cx, cy` を含む内部パラメータ行列K |
| `dist_coeffs` | レンズ歪み係数 |
| `image_size` | 校正画像サイズ |
| `square_size_mm` | 使用した1マスの実寸7.12 mm |
| `rms` | 全ビューの再投影RMS誤差、単位px |
| `per_view_errors` | 画像ごとの再投影誤差 |
| `camera_serial` | 左右取り違え防止用シリアル |

ここで「任意のpxを一律にmmへ変換」しているわけではない。既知の7.12 mmを使ってカメラの投影モデルを推定する。奥行きを含むmm単位の3D位置は、その後のステレオ三角測量で得られる。

## 7. 手順3: 同期ステレオ校正

### 7.1 左右同期画像を取得する

```powershell
.\venv\Scripts\python.exe stereo_checkerboard_sync_capture.py `
  --left-serial 00000508 `
  --right-serial 00000509 `
  --output-dir stereo_checkerboard_calib `
  --checkerboard-image checkerboard_10x7_normal.png `
  --square-mm 7.12 `
  --pose-count 40 `
  --duration-sec 0.2 `
  --hw-sync left-master `
  --frame-window-us-list 10000,20000,30000,40000 `
  --preferred-frame-window-us 20000
```

初期プレビューでは、チェッカーボードを移動した後に `R` を押すと、キューに残った古いイベントを捨ててカメラを開き直し、最新イベントから表示できる。`Enter` でそのposeを記録する。

採用された1 poseの対応は次のようになる。

```text
left/calib_images/pose_001.png
          ↕ 同じpose、同じ共通時間窓
right/calib_images/pose_001.png
```

`paired_candidates/pose_###/` は候補を調べるための補助出力であり、ステレオ校正の直接入力ではない。校正へ渡すのは左右の `calib_images` である。両側で同名のファイルだけが1ペアになる。

### 7.2 ステレオ外部パラメータを計算する

現在の最終結果を再現するコマンドは次のとおりである。

```powershell
.\venv\Scripts\python.exe stereo_camera_calibrate.py `
  --left-images stereo_checkerboard_calib_on_stage_20260728\left\calib_images `
  --right-images stereo_checkerboard_calib_on_stage_20260728\right\calib_images `
  --left-intrinsics checkerboard_calib_left_on_stage_20260727\single_calibration_square7p12_left00508.npz `
  --right-intrinsics checkerboard_calib_right_on_stage_20260728\single_calibration_square7p12_right00509.npz `
  --left-serial 00000508 `
  --right-serial 00000509 `
  --square-mm 7.12 `
  --require-square-mm 7.12 `
  --min-pairs 20 `
  --exclude-pairs pose_020 `
  --output stereo_checkerboard_calib_on_stage_20260728\stereo_calibration_square7p12_final.npz `
  --debug-dir stereo_checkerboard_calib_on_stage_20260728\stereo_debug_square7p12_final
```

ステレオNPZの主要パラメータ:

| キー | 意味 |
|---|---|
| `left_camera_matrix`, `left_dist_coeffs` | 左カメラの内部パラメータ |
| `right_camera_matrix`, `right_dist_coeffs` | 右カメラの内部パラメータ |
| `R`, `T` | 左カメラ座標から右カメラ座標への剛体変換 |
| `E`, `F` | 左右画像のエピポーラ幾何を表す行列 |
| `R1`, `R2`, `P1`, `P2`, `Q` | 平行化・視差復元用パラメータ |
| `stereo_rms` | ステレオ校正の再投影RMS、単位px |
| `accepted_pairs` | 実際に採用された左右ペア |
| `symmetric_epipolar_rms_px` | ペアごとの左右整合誤差 |
| `left_camera_serial`, `right_camera_serial` | 左右取り違え防止 |

現在の粒子3D処理は、歪み補正後の左右点と `R`, `T` を用いて直接三角測量し、左カメラ座標を出力する。

## 8. 手順5・6: 通常の同期記録と3D復元

### 8.1 左右イベントをハードウェア同期記録する

```powershell
$RunDir = "stereo_eventcam_records\stereo_event_20260728_example"

.\venv\Scripts\python.exe stereo_eventcam_record_sync.py `
  --left-serial 00000508 `
  --right-serial 00000509 `
  --run-dir $RunDir `
  --duration-sec 1.0 `
  --delta-t-us 1000 `
  --hw-sync left-master `
  --npz-compression none `
  --stereo-calibration stereo_checkerboard_calib_on_stage_20260728\stereo_calibration_square7p12_final.npz `
  --note "current on-stage calibration; hardware 200 mm"
```

主な出力:

```text
$RunDir/
├─ left/
│  ├─ left_events.npz
│  └─ left_recording_meta.json
├─ right/
│  ├─ right_events.npz
│  └─ right_recording_meta.json
└─ stereo_recording_manifest.json
```

プレビューで `R` を押すと古いイベントを捨てて開き直せる。実計測では `--hw-sync left-master` を使い、`--hw-sync off` は同期精度を評価する本番データには使わない。

### 8.2 2D追跡と3D三角測量を一括実行する

PAT粒子の静止・低速計測に使っている設定例:

```powershell
.\venv\Scripts\python.exe stereo_process_recording.py `
  $RunDir `
  --stereo-calibration stereo_checkerboard_calib_on_stage_20260728\stereo_calibration_square7p12_final.npz `
  --window-us 500 `
  --hop-us 500 `
  --dt-us 500 `
  --tracking-method event_weighted `
  --min-events 20 `
  --max-time-gap-sec 0.001
```

`stereo_process_recording.py` は次を順に行う。

1. 左NPZを `eventcam_npz_track.py` で2D追跡
2. 右NPZを同様に2D追跡
3. ハードウェア同期メタデータから右時刻offsetを自動決定
4. `stereo_triangulate_tracks.py` で3D化
5. `stereo_plot_3d_points.py` で確認図を作成

主な出力:

```text
$RunDir/
├─ left/event_tracking/
│  ├─ event_centres_raw.csv
│  └─ event_centres_interp.csv
├─ right/event_tracking/
│  ├─ event_centres_raw.csv
│  └─ event_centres_interp.csv
└─ stereo_3d/
   ├─ stereo_3d_points.npz
   ├─ stereo_3d_points.csv
   ├─ stereo_3d_points_summary.json
   └─ stereo_3d_trajectory.png
```

`stereo_3d_points.npz` の重要な内容:

| キー | 意味 |
|---|---|
| `t_sec` | 共通時刻 |
| `points_left_cam_mm` | 左カメラ座標の3D点 |
| `valid` | 三角測量が有効だった行 |
| `stereo_calibration` | 使用した校正ファイルの絶対パス |
| `stereo_calibration_sha256` | 使用した校正内容のハッシュ |

追跡条件を変えてやり直す場合は同じコマンドを再実行する。既存trackingをそのまま使って三角測量だけやり直す場合は `--skip-tracking` を付ける。

## 9. 手順4・7: PAT登録とPAT座標への変換

### 9.1 カメラ→PAT登録を作る

設定は [pat_stereo_grid_config.json](./pat_stereo_grid_config.json) にある。現在はPATのX/Y/Zそれぞれ3水準、合計27点で、非共面の既知点を使う。

前提:

- ステレオ校正を完了している。
- カメラをOssila stageの `hardware = 200.0 mm` に置く。
- Motion Consoleまたはreadbackで実際に200.0 mmであることを確認する。
- PAT/AcousToolsの `[0, 0, 0]` をPAT原点として使う。

まず動作計画だけを作る。

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_capture.py `
  --config pat_stereo_grid_config.json `
  --output-root pat_stereo_grid_records `
  --dry-run
```

表示されたsessionを確認し、その同じsessionを指定して取得する。`SESSION_DIR` は実際に表示されたパスへ置き換える。

```powershell
$PatSession = "pat_stereo_grid_records\SESSION_DIR"

.\venv\Scripts\python.exe pat_stereo_grid_capture.py `
  --config pat_stereo_grid_config.json `
  --session-dir $PatSession `
  --stage-hardware-readback-mm 200.0 `
  --process-each
```

各点をすでに `--process-each` で3D化した後、登録だけを計算する。

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_process.py `
  $PatSession `
  --skip-processing
```

主な出力:

```text
$PatSession/
├─ session_config.json
├─ pat_stereo_grid_manifest.json
├─ captures/p001 ... p027/
└─ registration/
   ├─ camera_to_pat_transform.npz
   ├─ camera_to_pat_transform.json
   ├─ camera_to_pat_transform_summary.txt
   ├─ grid_correspondences.csv
   └─ pat_camera_registration.png
```

- `.npz`: `stereo_apply_pat_transform.py` の入力に使う。
- `.json`: `stereo_stage_accuracy_config.json` から参照する。
- `grid_correspondences.csv`: 既知PAT点とステレオ実測点、残差の対応を確認する。

### 9.2 通常の3D結果をPAT座標へ変換する

```powershell
.\venv\Scripts\python.exe stereo_apply_pat_transform.py `
  $RunDir\stereo_3d\stereo_3d_points.npz `
  --transform pat_stereo_grid_records\pat_camera_registration_grid_27_20260728_144326\registration\camera_to_pat_transform.npz
```

既定の出力:

```text
$RunDir/stereo_3d/
├─ stereo_3d_points_pat.npz
├─ stereo_3d_points_pat.csv
└─ stereo_3d_points_pat_summary.json
```

PAT NPZには元の左カメラ座標に加えて `points_pat_mm` が保存される。初期安定点からの相対変位も必要なら `--relative-start` を付ける。

### 9.3 校正不一致エラーが出た場合

次のエラーは安全機構が正しく働いている。

```text
Stereo-calibration mismatch between the 3D points and PAT transform
```

解決方法は、PAT変換が参照しているものと同じステレオ校正で、元の同期イベントrunを `stereo_process_recording.py` により再処理することである。

`stereo_eventcam_records/stereo_event_20260721_143742` は、旧7.1 mm校正、旧物理配置、PC-clock同期のデータであり、現在のPAT変換とは組み合わせない。通常運用で `--allow-calibration-mismatch` を使って通過させてはいけない。このオプションは、結果を採用しない意図的な診断だけに限定する。

### 9.4 PAT変換を単独適用できるカメラ位置

現在の `camera_to_pat_transform.npz` は、登録した `hardware = 200 mm` のカメラ姿勢に対する変換である。ステージを動かした後のデータへ同じ平行移動をそのまま適用してはいけない。

ステージ距離精度実験では `stereo_stage_accuracy_analyze.py` が、各captureの実readbackに応じてカメラ→PATの平行移動を更新する。そのため、ステージ実験のデータは単独の `stereo_apply_pat_transform.py` ではなく、専用analyzerでPAT座標評価する。

## 10. 3D結果の表示

### 10.1 PAT座標の対話型viewerと実測登録済みCAD

PAT座標の軌跡と、2026-07-28の実測登録を合成したCADを重ねる:

```powershell
.\venv\Scripts\python.exe stereo_3d_viewer.py `
  --input $RunDir\stereo_3d\stereo_3d_points_pat.npz `
  --cad-obj drawings\openmpd_case_Twin_20260717.obj `
  --cad-config drawings\openmpd_case_Twin_20260728_pat_registered_overlay.json
```

このPAT表示では `--stereo-calibration` は必須ではない。この引数はカメラ座標系列に左右光学中心を表示するためのもので、`frame: "pat"` のCAD配置を決めるものではない。

`drawings/openmpd_case_Twin_20260717_cad_overlay.json` は旧来の公称軸合わせであり、今回の実測カメラ→PAT変換を含まない。旧設定は
`PAT X=-OBJ Z, PAT Y=OBJ X, PAT Z=-OBJ Y` としていたため、CAD上の焦点基準点
`[0,-12.92,0]` がPAT Z=`129.2 mm`へ置かれていた。現在のPATデータへは
`openmpd_case_Twin_20260728_pat_registered_overlay.json` を使う。

viewerの手動CAD offset/rotationはブラウザに保存される。現在はCAD設定ファイルの内容ハッシュも保存キーに含むため、JSONを更新した後に旧調整値は引き継がれない。同じJSONに対して手動調整を戻す場合はviewerの `Reset CAD` を押す。

viewerの既定視点は、PAT座標の `+X` が画面下、`+Z` が画面左、`+Y` が手前を向く配置である。操作は次のとおり。

- 左ドラッグ: 上下の極を越えられる自由な360度回転
- 右ドラッグ: 平行移動
- ホイール: 拡大・縮小
- `Fit`: 現在の表示角度を保ったまま全体を収める
- `Reset`: 既定視点へ戻して全体を収める
- 右上の `Top / Bottom / Right / Left / Front / Back`: 各軸方向からの正投影方向へジャンプ

右上のXYZ表示は現在の視点に追従する。標準視点は `Top=+Z`、`Bottom=-Z`、`Right=+X`、`Left=-X`、`Front=+Y`、`Back=-Y` から原点を見る定義である。

PAT grid登録結果そのものへCADを重ねる:

```powershell
.\venv\Scripts\python.exe stereo_3d_viewer.py `
  --input pat_stereo_grid_records\pat_camera_registration_grid_27_20260728_144326\registration\camera_to_pat_transform.npz `
  --cad-obj drawings\openmpd_case_Twin_20260717.obj `
  --cad-config drawings\openmpd_case_Twin_20260728_pat_registered_overlay.json
```

### 10.2 PAT座標の静止画

`stereo_plot_3d_points.py` は `--frame auto` が既定である。入力に `points_pat_mm` があればPAT座標を優先し、なければ左カメラ座標を使う。

```powershell
.\venv\Scripts\python.exe stereo_plot_3d_points.py `
  $RunDir\stereo_3d\stereo_3d_points_pat.npz `
  --frame pat
```

出力は `stereo_3d_trajectory_pat.png` で、軸は `PAT X/Y/Z [mm]` になる。

同じPAT NPZに含まれる元の左カメラ座標を明示的に描く場合:

```powershell
.\venv\Scripts\python.exe stereo_plot_3d_points.py `
  $RunDir\stereo_3d\stereo_3d_points_pat.npz `
  --frame left-camera `
  --range-mode calibration `
  --stereo-calibration stereo_checkerboard_calib_on_stage_20260728\stereo_calibration_square7p12_final.npz
```

## 11. 手順8: Ossilaステージ距離精度実験

### 11.1 現在の設定ファイルで先に修正する箇所

[stereo_stage_accuracy_config.json](./stereo_stage_accuracy_config.json) は、2026-07-28時点で次の2パスが未更新であるため、そのまま本実験を開始できない。

`camera.stereo_calibration`:

```json
"stereo_calibration": "stereo_checkerboard_calib_on_stage_20260728/stereo_calibration_square7p12_final.npz"
```

`analysis.camera_to_pat_transform`:

```json
"camera_to_pat_transform": "pat_stereo_grid_records/pat_camera_registration_grid_27_20260728_144326/registration/camera_to_pat_transform.json"
```

さらに、stage identity probe後に次の4つを実機の応答へ更新する。

| 設定キー | probe出力の対応項目 |
|---|---|
| `stage.expected_device_response` | `device` |
| `stage.expected_stage_serial_response` | `internal_serial` |
| `stage.expected_acceleration_mm_s2` | `acceleration_mm_s2` |
| `stage.expected_deceleration_mm_s2` | `deceleration_mm_s2` |

現在確認済みのprobe値は `G2010B1`, `5`, `99.972351`, `99.972351` だが、実行前にもう一度probeし、物理的にカメラを載せたY軸ステージであることを確認してから転記する。

### 11.2 ステージを識別する

接続候補とUSBシリアルを一覧表示する。これはステージを開かず、動かさない。

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py `
  --config stereo_stage_accuracy_config.json `
  --list-stage-ports
```

設定したY軸だけを開き、identity/settings/statusを読み、動かさずに閉じる。

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py `
  --config stereo_stage_accuracy_config.json `
  --execute `
  --probe-stage-identity
```

probe sessionは本実験には再利用しない。4つのexecution-lockを設定した後、新しいdry-plan sessionを作る。

### 11.3 dry planを作り、同じsessionを実行する

まず `--execute` なしで計画だけを作る。ハードウェアは開かれない。

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py `
  --config stereo_stage_accuracy_config.json `
  --output-root stereo_stage_accuracy_records
```

出力されたstation、hardware target、soft limit、サンプル数、校正・変換を確認する。現在の意図は次のとおりである。

```text
hardware: 200, 180, 160, ..., 20, 5 mm
相対距離:   0,  20,  40, ..., 180, 195 mm
soft limit: [5, 200] mm
```

dry planが最後に表示するコマンドをそのまま使い、**同じ `--session-dir`** を実行する。

```powershell
$StageSession = "stereo_stage_accuracy_records\DRY_PLANが表示したSESSION_DIR"

.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py `
  --config stereo_stage_accuracy_config.json `
  --session-dir $StageSession `
  --execute
```

必要なら次を追加できる。

- `--trace-cube-preview`: 取得前にPATで立方体の12辺をゆっくり描く。
- `--process-each`: 各capture直後に2D追跡・3D化する。
- `--no-preview`: 初期左右プレビューを省略する。
- `--home-depth-axis`: 絶対座標が未確立の場合だけhomeする。移動経路を物理確認し、`--yes` と併用しない。

本実験のfull `--execute` は、レビュー済みdry planのsessionがなければ拒否される。session内には校正、PAT変換、Ossila configのコピーとハッシュが保存され、実行途中で入力が変わったsessionは再開できない。

### 11.4 PAT座標で解析する

取得後:

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_analyze.py `
  $StageSession
```

`--process-each` ですでに全runを処理済みなら:

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_analyze.py `
  $StageSession `
  --skip-processing
```

出力先は既定で `$StageSession/analysis/` である。

```text
analysis/
├─ sample_measurements.csv
├─ pass_accuracy.csv
├─ station_accuracy.csv
├─ cube_edges.csv
├─ stereo_stage_accuracy_summary.json
├─ stereo_stage_accuracy_summary.png
└─ stereo_stage_accuracy_report.md
```

| 出力 | 内容 |
|---|---|
| `sample_measurements.csv` | capture単位の有効率、readback、観測位置、残差 |
| `pass_accuracy.csv` | cycle・station・往復単位の合否根拠 |
| `station_accuracy.csv` | 各距離での中心誤差、立方体誤差、成功pass率 |
| `cube_edges.csv` | 立方体12辺の長さ誤差 |
| `stereo_stage_accuracy_summary.json` | 校正、変換、理論値、全集計値 |
| `stereo_stage_accuracy_summary.png` | 距離に対する精度のグラフ |
| `stereo_stage_accuracy_report.md` | 実験結果と合否の読み方 |

専用analyzerは各captureの実際のOssila readbackを使い、登録時の変換を次のように更新する。

```text
t_current = t_at_registration
          + stage_axis_in_pat * (current_global - registration_global)
```

したがって、出力誤差はPAT座標で評価でき、途中で手作業によりカメラ座標へ戻す必要はない。

### 11.5 理論精度だけを先に表示する

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_analyze.py `
  --theory-only `
  --config stereo_stage_accuracy_config.json `
  --z-min-mm 200 `
  --z-max-mm 500 `
  --z-step-mm 20
```

理論値はステレオ校正の実際の収束幾何と、設定したpixel位置誤差から数値Jacobianで計算される。実測では粒子抽出、同期、校正残差、PATの平衡位置、振動、ステージ取付誤差も含まれるため、理論値との比較が必要である。

## 12. 距離の解釈

最近点 `hardware = 200 mm` における距離を `D0` とすると、ステージreadbackだけから確実に与えられるのは相対距離である。

```text
D_relative(h) = 200 - h
D(h) = D0 + (200 - h)
```

現在のPAT登録から自己推定された値は次のとおりである。

| 距離定義 | 自己推定値 |
|---|---:|
| PAT原点から左光学中心までの斜距離 | 218.873197 mm |
| PAT原点から左右光学中心を結ぶベースラインへの垂線長 `D0` | 210.454324 mm |

したがって、自己推定値を使う表示上の絶対距離は、おおよそ `210.454 mm` から `405.454 mm` までになる。

ただし、この `D0` は精度を評価する同じステレオ観測とPAT登録から求めた値であり、独立な絶対距離の真値ではない。ステージ変位195 mmの相対精度はOssila readbackを真値として評価できるが、「カメラからの絶対距離が何mmまで正しいか」を独立に検証するには、レーザー距離計、CMM、治具寸法など別原理の測定による `D0` と不確かさが必要である。

独立測定が得られたら `stereo_stage_accuracy_config.json` の次を設定する。

```json
"independent_absolute_reference": {
  "available": true,
  "definition": "pat_origin_to_stereo_optical_center_baseline_line_perpendicular",
  "distance_mm": 210.0,
  "standard_uncertainty_mm": 0.1,
  "method": "実際の測定方法",
  "instrument": "実際の測定器"
}
```

例の数値をそのまま使わず、実測値を記入する。

## 13. トラブル時の確認順

### チェッカーボードが検出されない

1. `checkerboard_10x7_normal.png` を表示しているか。
2. 内点数が9×6であるか。
3. 盤面全体が画面内にあり、小さすぎないか。
4. `corner_debug` で誤検出していないか。
5. `duration-sec` より、採用画像のイベント量と輪郭品質を確認する。

### ステレオ校正用画像がない

`paired_candidates` ではなく、次を確認する。

```text
left/calib_images/pose_###.png
right/calib_images/pose_###.png
```

### プレビューが古いイベントを追い続ける

プレビュー中に `R` を押してキューを捨て、最新イベントから開き直す。処理中の追い付き表示を避けるため、単眼取得で `--preview-during-processing` を付けない。

### PAT変換でcalibration mismatchになる

3D NPZの `stereo_calibration_sha256` とPAT変換の同キーを一致させる。同じ校正NPZを指定して元のrunを再処理する。`--allow-calibration-mismatch` で本番結果を作らない。

### stage実行がlockされる

1. calibration/PAT transformパスを現行ファイルへ更新する。
2. `--execute --probe-stage-identity` を実行する。
3. device、内部serial、acceleration、decelerationの4項目をconfigへ転記する。
4. 新しいdry planを作る。
5. dry planが表示した同じsessionを `--execute` する。

## 14. 現在の推奨入口

用途ごとの最初のコマンドは次のとおりである。

- 現在の校正で新しい粒子計測をする: `stereo_eventcam_record_sync.py`
- 取得済み同期runを3D化する: `stereo_process_recording.py`
- hardware 200 mmの3D結果をPAT座標にする: `stereo_apply_pat_transform.py`
- カメラ/PATを動かしたため登録を作り直す: `pat_stereo_grid_capture.py`
- Ossila距離精度実験を始める: configを更新後、`stereo_stage_accuracy_capture.py` のdry plan

# PAT - Stereo Camera 3D Grid Registration

> 2026-07-27更新: 単眼・ステレオ再校正を含む最新の入口は
> [CAMERA_PAT_RECALIBRATION_WORKFLOW_JP.md](CAMERA_PAT_RECALIBRATION_WORKFLOW_JP.md)
> です。正方形一辺は **7.12 mm** であり、旧値7.1 mmは使用しません。

## 目的

この手順は、AcousToolsのPAT座標で指定した静止粒子位置と、左右イベントカメラから復元した3D位置を対応付け、左カメラ座標からPAT座標への剛体変換を求めるものです。

最終的な変換は次式です。

```text
p_pat_mm = R_camera_to_pat @ p_left_camera_mm + t_camera_to_pat_mm
```

この登録後は、通常のステレオ計測結果をPAT座標へ変換できます。動的軌道とideal logを比較するときは、必要に応じて開始位置だけを相対原点としてそろえます。

## 現在の校正チェーン

1. 左カメラ内部校正: `checkerboard_calib_left`
2. 右カメラ内部校正: `checkerboard_calib_right`
3. 左右ステレオ校正:
   `stereo_checkerboard_calib/stereo_calibration_square7p12_left00508_right00509.npz`
4. PAT - カメラ座標登録: 本手順の3D格子計測

3D格子計測でも左右の対応条件を統一するため、左Master・右Slaveのハードウェア同期を必須にします。左右の抽出が別の物体やノイズを追っていないことも確認します。

## 追加したファイル

- `pat_stereo_grid_config.json`
  - 格子、PAT移動、カメラ、追跡、登録計算の設定
- `pat_stereo_grid_capture.py`
  - PAT移動と点ごとのステレオ記録
- `pat_stereo_grid_process.py`
  - 全点の2D追跡、3D復元、静止代表点抽出、カメラ→PAT登録
- `stereo_apply_pat_transform.py`
  - 通常の3D計測結果をPAT座標へ変換
- `stereo_3d_viewer.py`
  - ドラッグ、ズーム、色分け、再生に対応したローカル3Dビューア

格子計測は `stereo_eventcam_record_sync.py --run-dir ...` を呼び出し、両カメラで同じカメラ時刻区間を保存します。

## デフォルト格子

デフォルトはPAT/AcousTools中心 `[0, 0, 0] mm` の周囲に置いた27点です。

```text
X offset: -10, 0, +10 mm
Y offset:  -5, 0,  +5 mm
Z offset: -10, 0, +10 mm
```

実座標の範囲は次のとおりです。

```text
X: -10 ... +10 mm
Y:  -5 ...  +5 mm
Z: -10 ... +10 mm
```

格子は蛇行順に走査し、隣接点間の移動距離を抑えます。X、Y、Zのすべてに複数座標を持つ非共面格子なので、3次元の回転と並進を安定して求められます。

## 計測前の固定条件

- 左カメラは `00000508`、右カメラは `00000509` として設定済みです。
- 左右カメラ、レンズ、フォーカス、PAT本体を校正後に動かさないでください。
- カメラを動かした場合は、少なくともPAT - カメラ3D格子登録をやり直します。
- レンズ、フォーカス、カメラ間隔を変えた場合は、単眼内部校正とステレオ校正もやり直します。
- AcousTools/PAT `[0,0,0]` 付近で粒子を安定保持できる状態にします。物理的な設置高さ120 mmは制御座標へ加算しません。
- 27点すべてが安全な浮揚範囲であることを確認します。
- CAD図面は、軸方向、カメラ基線、概略距離、得られた並進の妥当性確認に使用します。

格子登録は、PATの指定座標と実際の静止平衡位置の差も残差に含む「運用上の登録」です。これは、これまで行っていた開始位置合わせと相対軌道評価の考え方と整合します。

## 1. 設定確認

最初に [pat_stereo_grid_config.json](pat_stereo_grid_config.json) を確認します。

特に確認する値:

```json
"center_mm": [0.0, 0.0, 0.0],
"acoustools_zero_in_pat_mm": [0.0, 0.0, 0.0],
"left_serial": "00000508",
"right_serial": "00000509",
"record_sec": 0.3,
"window_us": 500,
"hop_us": 500,
"dt_us": 500
```

設定JSON内のPAT座標はmmです。AcousToolsへ渡す値は
`(PAT座標 - acoustools_zero_in_pat_mm) * 1e-3` でmへ変換されます。

静止点の追跡では、デフォルトで次の閾値を使います。

```text
threshold_count = 1
min_events      = 20
min_area        = 5
min_mass        = 30
ROI             = full sensor
```

`500 us` 窓と `500 us` hopにより、0.3秒の記録から最大約600個の静止位置候補を得ます。動的軌道で使う細かいhopより処理量を減らしています。

## 2. ハードウェアを開かずに計画確認

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_capture.py --dry-run
```

27点のPAT座標と走査順が表示されます。このコマンドではPATとイベントカメラを開きません。

## 3. 3D格子を記録

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_capture.py
```

処理の順番:

1. PATへ接続し、設定中心を保持します。
2. 左右カメラのリアルタイムプレビューを一度だけ開きます。
3. Enterで粒子配置を確定します。
4. 各格子点へ最大0.25 mm刻みで移動します。
5. 0.8秒静定します。
6. 左右カメラを別プロセスで開き、0.3秒記録します。
7. 両カメラを完全に閉じてから次の点へ移動します。
8. 終了時に設定中心へ戻し、PAT停止フレームを送ります。

イベントカメラは点ごとに開閉します。PAT制御プロセスとMetavision記録プロセスを分離しているため、USBアクセス競合の影響を局所化できます。

初回は `--process-each` を付けず、27点の記録を先に完了する方法を推奨します。後処理が長くてもPATとカメラを占有し続けません。

## 4. 中断後の再開

セッションディレクトリは次の形式です。

```text
pat_stereo_grid_records/pat_camera_registration_grid_27_YYYYMMDD_HHMMSS
```

中断・失敗後は同じセッションを指定します。

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_capture.py `
  --session-dir pat_stereo_grid_records\pat_camera_registration_grid_27_YYYYMMDD_HHMMSS
```

`captured` または `processed` の点は飛ばし、未完了点から再開します。失敗した試行データは削除せず、`attempt_01`, `attempt_02` として残します。

1点が失敗しても残りを続ける場合:

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_capture.py `
  --session-dir pat_stereo_grid_records\pat_camera_registration_grid_27_YYYYMMDD_HHMMSS `
  --continue-on-error
```

## 5. 後処理とカメラ→PAT登録

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_process.py `
  pat_stereo_grid_records\pat_camera_registration_grid_27_YYYYMMDD_HHMMSS
```

各点について次を実行します。

1. 左カメラNPZを2D追跡
2. 右カメラNPZを2D追跡
3. ステレオ三角測量
4. 記録の先頭・末尾を各0.05秒除外
5. 3Dサンプルの外れ値をMADで除外
6. 残った3D位置の中央値を、その格子点のカメラ計測値とする
7. 全格子点から剛体変換を推定
8. 一部の点を推定に使わず、検証誤差を計算
9. 2 mmを超える対応点を既定では外れ点として記録

すでに全点の `stereo_3d_points.npz` がある場合:

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_process.py SESSION_DIR --skip-processing
```

各点の追跡・三角測量だけを先に終える場合:

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_process.py SESSION_DIR --process-only
```

## 6. 登録結果

主な出力:

```text
SESSION_DIR/
  pat_stereo_grid_manifest.json
  session_config.json
  captures/
  registration/
    camera_to_pat_transform.npz
    camera_to_pat_transform.json
    camera_to_pat_transform_summary.txt
    grid_correspondences.csv
    pat_camera_registration.png
```

`camera_to_pat_transform.npz` が機械処理用の正式な変換です。

`camera_to_pat_transform.json` には次が入ります。

- 回転行列 `R_camera_to_pat`
- 並進 `t_camera_to_pat_mm`
- 全点fit RMS
- 最大残差
- 検証点RMS
- 棄却された格子点
- similarity scale診断値

初期の実務的な確認目安:

- `target_rank = 3`
- similarity scale診断値が1に近い
- 棄却点が少数
- fit RMSとvalidation RMSが同程度
- validation RMSが1 mm以下なら良好な候補
- 1～2 mmなら対応点動画と静止散らばりを確認
- 2 mm超なら誤追跡、カメラ移動、PAT静止位置、左右対応を再確認

上記のmm閾値は現時点の運用目安です。実データを蓄積した後、静止再現性と目的とする動的評価精度に合わせて更新します。

similarity scale診断値はスケール誤差の点検専用です。保存する本変換は剛体変換であり、診断スケールは適用しません。1から数パーセントずれる場合は、チェッカーボードsquare size、ステレオ校正、格子指定値を確認します。

## 7. 通常の3D結果をPAT座標へ変換

```powershell
.\venv\Scripts\python.exe stereo_apply_pat_transform.py `
  stereo_eventcam_records\RUN\stereo_3d\stereo_3d_points.npz `
  --transform SESSION_DIR\registration\camera_to_pat_transform.npz
```

出力:

```text
stereo_3d_points_pat.npz
stereo_3d_points_pat.csv
stereo_3d_points_pat_summary.json
```

開始位置を相対原点にした変位も保存する場合:

```powershell
.\venv\Scripts\python.exe stereo_apply_pat_transform.py `
  stereo_eventcam_records\RUN\stereo_3d\stereo_3d_points.npz `
  --transform SESSION_DIR\registration\camera_to_pat_transform.npz `
  --relative-start
```

`--relative-start` は最初の0.05秒のPAT座標中央値を引きます。ideal logとの相対軌道比較に使えます。

## 8. ドラッグ可能な3D表示

カメラ座標の既存3D結果:

```powershell
.\venv\Scripts\python.exe stereo_3d_viewer.py `
  --input stereo_eventcam_records\RUN\stereo_3d\stereo_3d_points.npz
```

PAT座標へ変換した結果:

```powershell
.\venv\Scripts\python.exe stereo_3d_viewer.py `
  --input stereo_eventcam_records\RUN\stereo_3d\stereo_3d_points_pat.npz
```

PATと左右カメラのCADモデルを重ねる場合:

```powershell
.\venv\Scripts\python.exe stereo_3d_viewer.py `
  --input stereo_eventcam_records\RUN\stereo_3d\stereo_3d_points_pat.npz `
  --cad-obj drawings\openmpd_case_Twin_20260717.obj `
  --cad-config drawings\openmpd_case_Twin_20260728_pat_registered_overlay.json
```

現在の左カメラ座標の結果へ、CADの左右レンズ中心をステレオ校正の光学中心に合わせて重ねる場合:

```powershell
.\venv\Scripts\python.exe stereo_3d_viewer.py `
  --input stereo_eventcam_records\stereo_event_20260716_175740\stereo_3d\stereo_3d_points.npz `
  --cad-obj drawings\openmpd_case_Twin_20260717.obj `
  --cad-config drawings\openmpd_case_Twin_20260717_camera_overlay.json
```

CADはサイドバーの `OpenMPD rig` で表示を切り替え、`CAD opacity` で透過率を調整します。`frame: "pat"` の設定は絶対PAT座標だけに、`frame: "camera"` の設定は左OpenCVカメラ座標だけに有効です。開始位置を引いた相対PAT座標には重ねません。

格子登録そのもの:

```powershell
.\venv\Scripts\python.exe stereo_3d_viewer.py `
  --input SESSION_DIR\registration\camera_to_pat_transform.npz
```

ビューアでは次を切り替えられます。

- カメラ座標、PAT座標、相対PAT座標
- 時刻、速度、Z座標、単色による色分け
- `Trail`: 開始から現在位置までの軌跡と現在の粒子位置
- `Position`: 現在の粒子位置だけ
- 0.02x、0.05x、0.1x、0.25x、0.5x、1xの再生速度
- 点、軌跡、格子、軸、PAT目標点、カメラ中心
- 0.01%刻みの再生ヘッドと、実トラックの1サンプルずつの前後送り
- `Loop`を有効にした繰り返し再生
- CADのPosition X/Y/Z [mm]、Rotation X/Y/Z [deg]、相対Scaleによる微調整。値はブラウザごとに保持され、`Reset CAD`で初期の校正配置へ戻る。CADは点群と同じ表示座標 `(-X, -Y, +Z)` に変換し、左レンズ中心を左カメラ原点へ、右レンズ中心を赤い右カメラ中心へ一致させる
- 視点fit/reset

Three.jsとOrbitControlsはローカル同梱しているため、表示時にインターネット接続は不要です。

`drawings/openmpd_case_Twin_20260717_cad_overlay.json` はOBJの1 unitを10 mmとして図面のPAT軸へ置いた旧公称値です。具体的には `PAT X = -OBJ Z`、`PAT Y = OBJ X`、`PAT Z = -OBJ Y` であり、2026-07-28の実測カメラ→PAT登録を含みません。CAD上の焦点基準点 `[0,-12.92,0]` がPAT Z=`129.2 mm`へ置かれるため、現在のPATデータには使用しません。

現在の `drawings/openmpd_case_Twin_20260728_pat_registered_overlay.json` は、OBJの左右レンズ中心を現行ステレオ校正の左右光学中心へ合わせ、そのCAD→左カメラ変換へ実測の左カメラ→PAT変換を合成したものです。登録時のOssila hardware readbackは200 mmです。PAT座標の軌跡・格子登録結果へCADを重ねる場合はこちらを使用します。`--stereo-calibration` はカメラ中心表示用で、PAT-frame CAD配置そのものには影響しません。

`drawings/openmpd_case_Twin_20260717_camera_overlay.json` は、OBJ内の左右レンズ中心をそれぞれ左・右カメラの光学中心へ一致させます。現在はOBJの `[16.248, -12.92, 6.0]` を左カメラ`00000508`、`[16.248, -12.92, -6.0]` を右カメラ`00000509`として割り当てます。さらに、CAD上の両レンズから焦点中心へ向かう平均方向を、ステレオ校正から得た平均光軸方向へ合わせます。現OBJ/図面の公称基線120 mmと現行実校正基線120.138 mmの差は、`fit_baseline_scale: true` により約0.115%の一様スケール補正で合わせます。実機の125 mmは、両カメラのC/CSレンズマウントとレンズの接続部における中心間の機械寸法であり、ステレオ校正の光学中心間距離とは分けて扱います。現在のOBJは120 mm配置で出力されているため、125 mmの機械配置を外形まで厳密に反映するには、CAD側を125 mmで修正して再エクスポートするか、カメラ筐体を個別に移動するビューア補正が必要です。点群とCADには共通して左OpenCV座標の表示変換 `(-X, -Y, +Z)` を適用します。したがって左レンズ中心は左カメラ原点（cyan）、右レンズ中心は赤い右カメラ中心に一致します。CAD欄の`Anchor error L / R`でこの一致誤差を確認できます。この配置は現在のカメラ座標データを直ちにCADと比較するための公称可視化であり、PAT座標への登録精度は3D格子登録で確定します。

ステレオ3D NPZの座標と表示範囲はmmです。これはチェッカーボードの `square_size_mm=7.12` を使ったステレオ校正の並進量がmmであるためです。カメラ座標の3D表示だけは、OpenCV座標を `(-X, -Y, +Z)` に変換します。Yだけを反転すると座標系の利き手が変わるため、Xも同時に反転して右手系を保ちます。NPZ、CSV、三角測量結果そのものの座標値は変更しません。

ステレオ校正NPZが入力結果に紐づいている場合、ビューアは左カメラ光学中心を `[0, 0, 0] mm`、右カメラ光学中心を次式で求めます。

```text
C_right_in_left = -R^T @ T
```

今回の校正では、右カメラ中心は左カメラ座標でおよそ `[114.125, 0.739, 38.308] mm`、基線長は `120.385 mm` です。ビューア上では `[-114.125, -0.739, 38.308] mm` と表示されます。これは左カメラ基準の相対配置であり、PATやCADの絶対座標ではありません。

PAT - カメラ3D格子登録後は、カメラ→PAT剛体変換を使ってCADモデルをPAT座標へ置けます。CAD側にPAT原点、軸方向、単位mmが定義されていれば、GLBまたはOBJ形式を重ねることで、粒子・PAT・カメラの関係を同じ3D画面で確認するデジタルツイン表示へ拡張できます。

## 9. 通常の動的計測フロー

1. `stereo_eventcam_record_sync.py` で左右イベントをハードウェア同期記録
2. `stereo_process_recording.py` で左右2D追跡とカメラ座標3D復元
3. `stereo_apply_pat_transform.py` でPAT座標へ変換
4. 必要なら `--relative-start` で初期位置をそろえる
5. AcousTools ideal logと時間・相対位置を比較
6. `stereo_3d_viewer.py` とオーバーレイ動画で3Dジャンプの原因を確認

ideal logをカメラ→PAT登録の入力には使いません。静止格子で固定変換を求めた後、ideal logは動的追従誤差の評価対象として使います。

## 10. トラブル時の確認

### イベント数が少ない

- 粒子輪郭が両カメラで見えているか確認
- 反射・照明・点滅条件を確認
- `record_sec` を0.5秒へ延長
- 完全静止でイベントが出ない場合は、粒子位置を変えずに照明変調を検討

### LibUSB access error

- Metavision Viewerなど、カメラを保持している別プロセスを閉じる
- 純正USBケーブルと接続ポートを維持
- 同じセッションで再開し、失敗試行を上書きしない

### NonMonotonicTimeHigh

- 点ごとの開閉後に再開待ち時間を増やす
- `camera_reopen_wait_sec` を1.5～2.0秒へ増やす
- プレビューを一度だけに保つ
- 長時間の連続デュアルプレビューを格子計測中に開かない

### 後処理が長い

- 静止格子では既定の `window_us=500`, `hop_us=500`, `dt_us=500` を維持
- `--process-each` を使わず、計測後にまとめて処理
- 完了済みの点は自動再利用
- 再計算が必要な場合だけ `--force-processing`

### 登録残差が大きい

- `grid_correspondences.csv` の `scatter_rms_mm` と `registration_residual_mm` を確認
- 対応点の左右オーバーレイ動画を作り、別ノイズを追っていないか確認
- 特定点だけ悪い場合は、その点だけ再記録してセッションを再処理
- 多数点が系統的に悪い場合は、カメラ固定、ステレオ校正、PAT中心、軸方向を確認

## 11. バックアップ

最低限保存するもの:

```text
checkerboard_calib_left/
checkerboard_calib_right/
stereo_checkerboard_calib/
pat_stereo_grid_config.json
SESSION_DIR/
```

コード再現性のため保存するもの:

```text
stereo_eventcam_record_sync.py
stereo_process_recording.py
stereo_triangulate_tracks.py
pat_stereo_grid_capture.py
pat_stereo_grid_process.py
stereo_apply_pat_transform.py
stereo_3d_viewer.py
stereo_3d_viewer/
PAT_STEREO_3D_GRID_WORKFLOW.md
```

CAD図面が完成したら、カメラの光学中心・向き、PAT原点・軸、左右カメラ基線をこの登録結果と照合します。CADは格子登録の代替ではなく、軸取り違えや大きな幾何誤差を見つける独立した検算として有効です。

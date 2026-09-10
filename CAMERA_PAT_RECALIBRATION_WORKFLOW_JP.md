# カメラ再校正からPAT座標登録まで

## 最重要の固定条件

> **今回のチェッカーボードの正方形一辺は 7.12 mm です。旧値 7.1 mm は使用禁止です。**
>
> 本手順では
> `--square-mm 7.12 --require-square-mm 7.12` を指定します。誤って
> `--square-mm 7.1` とすると、校正計算前にエラーで停止します。

使用画像は `checkerboard_10x7_normal.png` です。
`checkerboard_10x7_blink_5Hz.gif` は使用しません。

| 項目 | 固定値 |
| --- | --- |
| 左カメラ | `00000508` |
| 右カメラ | `00000509` |
| 左単眼出力 | `checkerboard_calib_left` |
| 右単眼出力 | `checkerboard_calib_right` |
| ステレオ出力 | `stereo_checkerboard_calib` |
| 正方形一辺 | `7.12 mm` |
| 内部コーナー | `9 x 6` |
| PAT登録時のステージ | Ossila hardware `200 mm`（PATに最も近い基準位置） |
| PAT/AcousToolsソフトウェア原点 | `[0,0,0]` |

表示倍率やOSスケーリングを変えると実寸が変わります。校正直前にも画面上の
一辺が実測 `7.12 mm` であることを確認してください。

## 今回の判断と座標チェーン

PATをZ方向へ平行移動したことだけではカメラ内部パラメータや左右カメラ間の
外部パラメータは変わりません。一方、カメラをステージへ載せ替えた際に左右の
相対姿勢、フォーカス、固定応力が変わった可能性があるため、今回は単眼校正から
やり直す判断が安全です。

```text
左OpenCVカメラ座標
    └─ 新しいステレオ校正で3D復元
        └─ 27点の対応点で剛体登録
            └─ PAT座標
                └─ PAT YとOssilaステージYの方向対応は確認済み
```

カメラからPATへの変換は次式です。

```text
p_pat_mm = R_camera_to_pat @ p_left_camera_mm + t_camera_to_pat_mm
```

本手順ではAcousTools指令座標をそのままPAT座標として扱います。

```text
p_acoustools_m = p_pat_mm * 1e-3
```

したがってPAT中心とAcousTools指令はともに `[0,0,0]` です。PAT中心とカメラを
物理的に床・台座から120 mmの高さへ置いたことは、必要なら設置メタデータや
CAD照合値として記録しますが、PAT制御座標へ120 mmを加算しません。カメラと
PATの実際の相対位置は、後段の剛体登録の並進 `t_camera_to_pat_mm` が推定します。

## 0. 機械条件を固定する

1. 左右カメラ、レンズ、フォーカス、基線、ステージ上の固定を完了します。
2. PATのZ方向移設を完了し、その後はPAT本体を動かしません。
3. PAT座標登録時は、ステージをOssila hardware `200 mm`
   （PATに最も近い基準位置）へ置き、実際のreadbackを記録します。厳密な
   `200 mm` でend switchがactiveになる場合は、後述の安全確認が終わるまで
   `195 mm` などの安全な基準位置を使い、そのreadback値を基準として残します。
4. 同期ケーブルがMasterのSYNC_OUTからSlaveのSYNC_INへ接続されていることを
   確認します。既定は左Masterです。
5. デバイス一覧を確認します。

```powershell
.\venv\Scripts\python.exe stereo_eventcam_record_sync.py --list-devices
```

## 1. 左カメラの単眼画像を取得する

```powershell
.\venv\Scripts\python.exe eventcam_checkerboard_calibration_capture.py `
  --serial 00000508 `
  --output-dir checkerboard_calib_left_[名前を指定] `
  --checkerboard-image checkerboard_10x7_normal.png `
  --square-mm 7.12 `
  --require-square-mm 7.12 `
  --duration-sec 0.6 `
  --frame-window-us 20000 `
  --frame-window-us-list 10000,20000,30000,40000 `
  --render-mode all `
  --candidate-count 4
```

液晶のリフレッシュ、画素反転駆動、バックライト変調によって十分なイベントが
発生し、コーナーが安定検出できる場合は、`normal.png` を静止表示したまま
記録します。画像を動かす必要はありません。イベント数不足またはコーナー検出
失敗が続く場合だけ、露光・表示条件の調整や小さな移動をフォールバックとして
検討します。

中央だけでなく画面四隅、近距離・遠距離、roll・pitch・yawを変え、盤面全体が
画像内に入る姿勢を30～50程度取得します。

## 2. 右カメラの単眼画像を取得する

```powershell
.\venv\Scripts\python.exe eventcam_checkerboard_calibration_capture.py `
  --serial 00000509 `
  --output-dir checkerboard_calib_right_[名前を指定] `
  --checkerboard-image checkerboard_10x7_normal.png `
  --square-mm 7.12 `
  --require-square-mm 7.12 `
  --duration-sec 0.6 `
  --frame-window-us 20000 `
  --frame-window-us-list 10000,20000,30000,40000 `
  --render-mode all `
  --candidate-count 4
```

単眼画像は左右で同じ瞬間・同じ姿勢である必要はありません。左右それぞれで
画面全体を十分に覆うことが重要です。

## 3. 左右の単眼内部校正を計算する

左:

```powershell
.\venv\Scripts\python.exe single_camera_calibrate.py `
  --images checkerboard_calib_left_[名前を指定]\calib_images `
  --camera-serial 00000508 `
  --square-mm 7.12 `
  --require-square-mm 7.12 `
  --output checkerboard_calib_left_[名前を指定]\single_calibration_square7p12_left00508.npz `
  --debug-dir checkerboard_calib_left_[名前を指定]\calibration_debug_square7p12
```

右:

```powershell
.\venv\Scripts\python.exe single_camera_calibrate.py `
  --images checkerboard_calib_right_[名前を指定]\calib_images `
  --camera-serial 00000509 `
  --square-mm 7.12 `
  --require-square-mm 7.12 `
  --output checkerboard_calib_right_[名前を指定]\single_calibration_square7p12_right00509.npz `
  --debug-dir checkerboard_calib_right_[名前を指定]\calibration_debug_square7p12
```

確認項目:

- debug画像の全採用姿勢で9 x 6コーナーが正しいこと。
- CSVの一部の姿勢だけが極端に大きな誤差になっていないこと。
- 画面中央だけの似た姿勢に偏っていないこと。
- 旧実験のRMS約 `0.18～0.19 px` は参考値であり、今回の合否を自動的に
  決める閾値ではないこと。

## 4. 同期したステレオ対応画像を取得する

液晶リフレッシュで発生する左右イベントを同じ姿勢で使うため、左右を順番に撮る
旧 `stereo_checkerboard_calibration_capture.py` は使用しません。
今回追加したスクリプトは `stereo_eventcam_record_sync.py` を各姿勢で実行し、
左右で同じハードウェアカメラ時刻窓から校正画像を選びます。

```powershell
.\venv\Scripts\python.exe stereo_checkerboard_sync_capture.py `
  --left-serial 00000508 `
  --right-serial 00000509 `
  --output-dir stereo_checkerboard_calib_[名前を指定] `
  --checkerboard-image checkerboard_10x7_normal.png `
  --square-mm 7.12 `
  --pose-count 30 `
  --hw-sync left-master `
  --duration-sec 0.8 `
  --frame-window-us-list 10000,20000,30000,40000
```

左右両方で同じ時刻窓に9 x 6コーナーが見つかった場合だけ、同名の
`pose_###.png` が左右の `calib_images` に保存されます。

## 校正でmmスケールが決まる仕組み

`7.12 mm` は単純な一定の「1 pxあたり何mm」という換算係数ではありません。
透視投影では同じ1 pxでも奥行によって実寸が変わります。

- 単眼校正は、画像上のコーナー座標pxと、`7.12 mm`間隔で作った盤面上の3D点を
  対応付けます。焦点距離 `fx, fy` はpx、歪み係数は無次元、各盤面姿勢の並進
  `tvec` はmmになります。
- ステレオ校正は同じ7.12 mmスケールから左右光学中心間の並進 `T`、すなわち
  基線長をmmで求めます。
- ステレオ三角測量では概略
  `Z_mm = fx_px * baseline_mm / disparity_px` となるため、出力3D座標がmmに
  なります。

したがって「px→mm変換が校正チェーンで成立する」という理解は概ね正しいですが、
一定倍率による画像全体の一括換算ではなく、内部パラメータ・歪み・基線・視差を
使う3D幾何変換です。

## 5. ステレオ校正を計算する

最初はtrialとして計算します。

```powershell
.\venv\Scripts\python.exe stereo_camera_calibrate.py `
  --left-images stereo_checkerboard_calib_[名前を指定]\left\calib_images `
  --right-images stereo_checkerboard_calibright_[名前を指定]\right\calib_images `
  --left-intrinsics checkerboard_calib_left_[名前を指定]\single_calibration_square7p12_left00508.npz `
  --right-intrinsics checkerboard_calib_right_[名前を指定]\single_calibration_square7p12_right00509.npz `
  --left-serial 00000508 `
  --right-serial 00000509 `
  --square-mm 7.12 `
  --require-square-mm 7.12 `
  --min-pairs 20 `
  --output stereo_checkerboard_calib_[名前を指定]\stereo_calibration_square7p12_trial.npz `
  --debug-dir stereo_checkerboard_calib_[名前を指定]\stereo_debug_square7p12_trial
```

CSVには左右PnP RMSとsymmetric epipolar RMSが記録されます。明らかな外れ姿勢が
あれば画像を削除せず、たとえば次のように除外して最終計算します。

```powershell
.\venv\Scripts\python.exe stereo_camera_calibrate.py `
  --left-images stereo_checkerboard_calib_[名前を指定]\left\calib_images `
  --right-images stereo_checkerboard_calib_[名前を指定]\right\calib_images `
  --left-intrinsics checkerboard_calib_left_[名前を指定]\single_calibration_square7p12_left00508.npz `
  --right-intrinsics checkerboard_calib_right_[名前を指定]\single_calibration_square7p12_right00509.npz `
  --left-serial 00000508 `
  --right-serial 00000509 `
  --square-mm 7.12 `
  --require-square-mm 7.12 `
  --min-pairs 20 `
  --exclude-pairs pose_012,pose_027,[外したいpose] `
  --output stereo_checkerboard_calib_[名前を指定]\stereo_calibration_square7p12_left00508_right00509.npz `
  --debug-dir stereo_checkerboard_calib_[名前を指定]\stereo_debug_square7p12_final
```

外れがなければ `--exclude-pairs` の行を省略します。

確認項目:

- 使用可能な同期ペアが20以上あること。
- stereo RMSとepipolar RMSに少数の極端な外れがないこと。
- 基線長が実機の左右光学中心間隔と概ね一致すること。
- `T` の向きが左右カメラの物理配置と矛盾しないこと。

旧校正のstereo RMS `0.323912 px`、基線 `120.385 mm` は参考値です。

## 6. 左カメラ座標からPAT座標への剛体変換を求める

`stereo_apply_pat_transform.py` は、すでに求めた変換を適用するスクリプトです。
変換推定には `pat_stereo_grid_capture.py` と `pat_stereo_grid_process.py` を使います。

`pat_stereo_grid_config.json` のPAT中心とAcousTools原点は、どちらも
`[0,0,0]` に設定済みです。27点の範囲は次のとおりです。

```text
PAT:        X=-10..+10, Y=-5..+5, Z=-10..+10 mm
AcousTools: X=-10..+10, Y=-5..+5, Z=-10..+10 mm
```

点間移動は `acoustools_multitraj_no_eventcam.py` と同じ線形補間を使い、
最大 `0.25 mm/step`、`0.04 s/step` です。静止粒子抽出は
`event_weighted`、左右記録は左Master・右Slaveのハードウェア同期です。

ステージがOssila hardware `200 mm` の近距離基準にある状態で計画を確認します。
`pat_stereo_grid_capture.py` 自体はOssilaを動かさないため、sessionメモにも
hardware readbackを記録してください。end switchがactiveになる実機では、
検証済みの安全な最大位置を使います。dry-runにはPAT座標と実際のAcousTools
指令座標が両方表示されます。

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_capture.py `
  --dry-run `
  --stage-hardware-readback-mm 200.000
```

計測:

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_capture.py `
  --stage-hardware-readback-mm 200.000
```

`200.000`は例です。Ossila console/statusに表示された実際の数値へ置き換えてください。
これは自由記述のメモだけでなく、session manifest、登録NPZ/JSON/summaryへ数値として
引き継がれます。設定値200 mmとの差が0.5 mmを超える場合は収録前に停止します。

登録計算:

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_process.py `
  pat_stereo_grid_records\pat_camera_registration_grid_27_YYYYMMDD_HHMMSS
```

主出力:

```text
registration/camera_to_pat_transform.npz
registration/camera_to_pat_transform.json
registration/camera_to_pat_transform_summary.txt
registration/grid_correspondences.csv
registration/pat_camera_registration.png
```

初期品質条件は、使用点20以上、inlier率0.8以上、fit RMS 1 mm以下、
fit最大残差2 mm以下、validation RMS 1.5 mm以下、similarity scale誤差3%以下です。
失敗時も診断用変換は保存されますが終了コード4となり、
`registered_quality_failed` が記録されます。

## 7. 通常の3D結果をPAT座標へ変換する

```powershell
.\venv\Scripts\python.exe stereo_apply_pat_transform.py `
  stereo_eventcam_records\RUN\stereo_3d\stereo_3d_points.npz `
  --transform pat_stereo_grid_records\SESSION\registration\camera_to_pat_transform.npz
```

このスクリプトは、回転行列が正しい剛体回転であることと、3D復元とPAT登録が
同じステレオ校正ファイルを使用したことを検査します。

## 8. ステージ距離精度実験へ進む条件

`stereo_stage_accuracy_config.json` は、新しい7.12 mm校正と
`AcousTools [0,0,0] = PAT [0,0,0]` の定義へ更新済みです。
新しい最終NPZが存在し、PAT登録の品質条件を通過してから進みます。

単眼・ステレオ校正をhardware `200 mm` で行って構いませんが、そこへ固定する
理論上の必要はありません。チェッカーボード校正の入力は、画像上のcorner座標と
一辺 `7.12 mm` のボード座標であり、Ossila位置やカメラとPATの距離は校正NPZへ
入りません。カメラをステージへ固定し、左右相対姿勢、フォーカス、絞りをその後
変えないことの方が重要です。実験のprovenanceとして校正時のhardware readbackを
メモしておくのは有用ですが、metric scaleを決める入力ではありません。

一方、PAT座標登録を近距離基準のhardware `200 mm` で行うことには意味があります。
この位置で求めた変換から、PAT原点の左カメラ座標を逆変換で求められます。

```text
p_pat = R_camera_to_pat @ p_camera + t_camera_to_pat
p_camera(PAT原点) = -R_camera_to_pat.T @ t_camera_to_pat
camera Z estimate = p_camera(PAT原点)[2]
left optical-centre slant-range estimate = norm(p_camera(PAT原点))
C_right_in_left = -R_stereo.T @ T_stereo
b = normalize(C_right_in_left)
D0 estimate = norm(p_camera(PAT原点) - b * dot(p_camera(PAT原点), b))
```

`pat_stereo_grid_process.py` はこれらを
`pat_origin_left_camera_mm`、`pat_origin_left_camera_z_mm`、
`pat_origin_left_camera_slant_range_mm`、
`pat_origin_to_stereo_baseline_perpendicular_range_mm` として
NPZ/JSON/summaryへ保存します。
これは質問にある「PAT `[0,0,0]` を見たときのカメラからの距離」の**ステレオ
推定値**として使用できます。

今回の `D0` は、左カメラ光学中心からPAT原点までの斜距離ではなく、PAT原点から
左右光学中心を通る校正済みベースライン直線までの最短距離と定義します。校正結果の
ベースラインが左カメラX軸と完全に一致しなくても、実際の `R_stereo, T_stereo`
から直線を作るため、X軸平行を仮定せず計算できます。垂線の足が左右光学中心の間に
あるか、その位置も診断値として保存します。

ただしステージ距離精度実験では目的を分けます。

- ステージ移動量に対する相対距離精度、立方体寸法、再現性だけを評価するなら、
  基準位置からのOssila readback差が真値になるため、校正時のPAT距離は不要です。
- 「左カメラから実距離何mmまで正確か」を絶対距離で主張するなら、最終設置後・
  ステージ基準位置における独立な距離基準が必要です。ステレオ計測値や、その
  ステレオ点からfitしたカメラ→PAT変換だけを真値にすると、基準位置の定常biasを
  登録並進が吸収します。

ここでいう「独立な距離基準」とは、評価対象のステレオ計測とは別の方法で求めた
`D0`です。例えば、カメラ筐体・光学中心・PAT中心の治具寸法、レーザー測長、
外部トラッカー、CMMなどです。同じステレオ点から求めた `D0` は距離の推定結果
としては有効ですが、それ自身の誤差を判定するための真値にはできません。独立基準が
ない場合も、Ossila readback差に対する相対変位精度、直線性、再現性、立方体寸法は
評価できます。

絶対距離用の独立基準は、次のどれを指すか定義して実験開始時に記録します。

- 左カメラZ: 左光学中心を原点とする光軸方向の符号付き座標
- 光学中心距離: `sqrt(X²+Y²+Z²)` で求める左光学中心から粒子までの直線距離
- ステージ軸距離: 左光学中心から見たステージ軸方向の投影距離

3つはカメラ光軸、ステージ軸、PAT中心が同一直線上にあるときだけ一致します。
巻尺、治具寸法、レーザー距離計、外部トラッカーなど、測定方法、光学中心までの
治具offset、不確かさも併記します。物理高さ120 mmはこの距離とは別の設置寸法です。

### hardware 200 → 5 mmの走査

実可動域が約210 mmでhardware `200 mm`を通常測定点として使用できることが確認
されたため、soft limitを `[5,200] mm`、referenceをhardware `200 mm`へ変更済み
です。forward passは次の順番で実行します。

```text
hardware: 200, 180, 160, 140, 120, 100, 80, 60, 40, 20, 5 mm
global:       0, -20, -40, -60, -80,-100,-120,-140,-160,-180,-195 mm
```

reverse passは `5 → 200 mm` です。0 mmはhome端に近く危険なため測定点にしません。

ステージ移動がベースラインへの垂線方向と一致し、回転や横ずれがない場合、実距離は
次式になります。

```text
D(h) = D0 + (200 - h)
D(5) = D0 + 195 mm
```

軸が斜めの場合はこの単純加算ではなく、各点から校正済みベースライン直線までの
垂線距離を3Dベクトルで計算します。解析は両方を混同しないよう、Ossila相対変位と
ベースライン垂線距離推定を別列に保存します。

`stage.reference_global_mm` と `analysis.reference_stage_global_mm` が異なる設定は、
収録と解析の基準がずれるため、現在はvalidationで実行前に拒否します。

ステージ実験ではカメラ自体がPAT Y方向へ動くため、基準位置で求めた
カメラ→PAT変換の並進を全ステージ位置へ固定適用できません。既存の専用
registration passの8頂点fitを残し、各位置を基準に対する相対値として評価します。
解析CSV/JSONにはOssila hardware command/readback、ステレオ推定のcamera Z、
左光学中心からの直線距離、PAT点からステレオベースライン直線までの垂線距離を
別々に保存します。独立真値が未設定の間は、レポートに絶対距離PASSを出しません。
これはカメラZとPAT Yを同一視する処理ではありません。

実行前計画:

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py
```

ハードウェア実行には、従来どおりprobeで取得した内部serial、acceleration、
decelerationを実行ロックへ設定し、新しいdry planを作ってから `--execute`
を付けます。

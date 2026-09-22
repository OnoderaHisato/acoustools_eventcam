# ステレオイベントカメラ単独計測キット

2026-09-16版。**AcousTools・OpenMPD・PyTorchを使わず、左右カメラの校正から同期撮影、2D粒子抽出、3D復元まで実行するキット**です。

必要なプロジェクト固有Pythonコードはすべてこのディレクトリにあります。親ディレクトリのスクリプト、既存校正、計測データは参照しません。Python、NumPy、OpenCV、Matplotlib、実機撮影用OpenEBとカメラドライバは別途インストールします。SDKやvenvのバイナリは同梱していません。

最初に [SETUP_OPENEB_JP.md](SETUP_OPENEB_JP.md) で環境を準備してください。校正済みの状態から1回の計測を通す手順は [RECORDING_RUNBOOK_JP.md](RECORDING_RUNBOOK_JP.md)、カメラ1台だけで撮影・粒子抽出・動画化を行うサンプルは [MONO_SAMPLE_JP.md](MONO_SAMPLE_JP.md) にあります。以下のPowerShellコマンドは、**このディレクトリへ移動してから、その中に作成したvenv**で実行します。

## 1. 実行入口

共通入口は `eventcam_workflow.py`、設定は `workflow_config.json` です。

| コマンド | 処理 | カメラ |
|---|---|---|
| `doctor` | NumPy・OpenCV・Matplotlibの読み込み確認 | 開かない |
| `doctor --sdk` | OpenEB/HAL/UIの読み込みも確認 | 開かない・列挙しない |
| `devices` | カメラのデバイス一覧を取得 | SDKでデバイスを列挙 |
| `board` | 10×7マスの静止チェッカーボードPNGを作成 | 開かない |
| `capture-left` / `capture-right` | 各眼の単眼校正画像を取得 | 開く |
| `calibrate-left` / `calibrate-right` | 単眼内部校正を計算 | 開かない |
| `capture-stereo` | 同期した左右校正画像を取得 | 開く |
| `calibrate-stereo` | ステレオ外部校正を計算 | 開かない |
| `record` | 同期イベントを記録、校正・設定をrunへ保存 | 開く |
| `process --run 名前` | 保存済みrunの左右2D抽出と3D復元 | 開かない |
| `record-process` | 撮影に続けて粒子抽出・3D復元 | 開く |
| `all` | 校正画像取得から撮影・粒子抽出まで順に実行 | 開く |

すべてのコマンドに `--dry-run` を付けられます。呼出し予定だけを表示し、カメラSDKをimportせず、子プロセスもファイル書込みも行いません。これから生成する入力ファイルの存在確認は省略します。実機の接続や保持、校正品質を保証する確認ではありません。

## 2. 固定条件と設定

| 項目 | 既定値 |
|---|---|
| 左カメラ | `00000508`、Master |
| 右カメラ | `00000509`、Slave |
| 配線 | 左SYNC_OUT → 右SYNC_IN |
| センサー | 1280×720 |
| チェッカーボード | 10×7マス、内部コーナー9×6 |
| 正方形の実測一辺 | **7.12 mm**。7.1 mmは受け付けない |
| セッション保存先 | `data/session_cal20260805`（持込み校正入り。下記参照） |
| ステレオ校正姿勢数 | 30。最低採用ペア数20 |
| 通常撮影時間 | 2秒。`record_duration_sec`で変更。`0`で2回目の`Enter`まで手動停止 |
| 2D抽出 | イベント重み付き重心 `event_weighted` |
| 積算窓／推定間隔／補間間隔 | 200／100／100 µs |
| 最大補間欠落時間／最大位置ジャンプ | 5 ms／15 px |
| 最大再投影誤差 | 3 px |
| 座標出力 | 左カメラ座標、mm |

`workflow_config.json`を編集して実験条件を設定します。serialは先頭ゼロを含む文字列です。設定キーの不足・綴り間違い、同一serial、左Master以外の同期、ROI範囲外、非正の撮影時間等を検査します。

`session_dir`はこのキット内部の相対パスにします。新しい校正実験では、例えば `data/session02` へ変更します。同じセッションで撮影を繰り返す場合は、異なる `--run` 名を使います。名前を省略した撮影では日時付きのrun名を生成します。

### 2-1. 同梱の持込み校正で先に撮影する

既定の `data/session_cal20260805/calibration/` には、親プロジェクトで現在使用中のステレオ校正を持ち込んであります。出所と条件は同ディレクトリの `IMPORTED_CALIBRATION.txt` に記載しています。

| 項目 | 値 |
|---|---|
| コピー元 | `stereo_checkerboard_calib_extrinsics_20260805/stereo_calibration_square7p12_extrinsics_final.npz` |
| 取得日 | 2026-08-05 |
| serial | left `00000508` / right `00000509` |
| 画像サイズ／マス | 1280×720 ／ 7.12 mm、内部コーナー9×6 |
| 採用ペア数 | 23 |
| stereo_rms | 0.362 px |
| 基線長 | 120.53 mm |

このため、**`board` から `calibrate-stereo` までを飛ばして `record` から始められます。**

```powershell
.\venv\Scripts\python.exe .\eventcam_workflow.py record --run trial01 --dry-run
.\venv\Scripts\python.exe .\eventcam_workflow.py record --run trial01
.\venv\Scripts\python.exe .\eventcam_workflow.py process --run trial01
```

`record` は実行前にこの校正をserial・画像サイズ・マス寸法・回転行列の直交性・基線長について検査し、runへコピーとハッシュを保存します。

この校正は**撮影当時の左右カメラ配置に対応します**。カメラの固定、レンズ、フォーカス、相対姿勢が変わっていれば無効で、3D座標が静かに誤ります。別の場所・別の機材へキットを移した場合は、この校正を使わず、新しい `session_dir`（例 `data/session02`）で `board` から校正し直してください。このセッションで `calibrate-stereo` を実行しようとすると、持込み校正を保護するため `require_fresh` が拒否します。

抽出パラメータ（窓200 µs、hop 100 µs、dt 100 µs、`event_weighted` 等）は親プロジェクトの現行運用値と一致させてあります。違いは左眼のLEDマスクだけで、このキットはPAT開始LEDを使わないため `left_mask_roi` は空です。PATを動かしながら撮影してLEDが左眼に写り込む場合は、LEDを粒子と誤認しないよう `left_mask_roi` に `600,0,1280,180` を設定してください。

**このキットではPAT開始LEDは使いません。** PATの運動開始時刻との対応付け、PAT座標変換、理想指令比較も行いません。左右同期だけで3Dを復元します。LED領域の既定除外は左右とも空です。別の発光物を除きたい場合に限り `processing.left_mask_roi` / `right_mask_roi` を明示します。元プロジェクトの左LED設定や既存校正ファイルは変更していません。

## 3. 全工程を続けて実行する

この節はキット自身で校正からやり直す手順です。**先に `session_dir` を未使用の名前（例 `data/session02`）へ変更してください。** 同梱の `data/session_cal20260805` には持込み校正が入っているため、`calibrate-stereo` の出力先が既存となり、カメラを開く前に `require_fresh` が拒否します。持込み校正のまま撮影するだけなら §2-1 へ進んでください。

まず環境と実行予定を確認します。

```powershell
.\venv\Scripts\python.exe .\eventcam_workflow.py doctor
.\venv\Scripts\python.exe .\eventcam_workflow.py doctor --sdk
.\venv\Scripts\python.exe .\eventcam_workflow.py all --run sample01 --dry-run
```

初回はチェッカーボードを先に生成します。

```powershell
.\venv\Scripts\python.exe .\eventcam_workflow.py board
```

生成された `checkerboard_10x7_normal.png` を表示し、表示倍率・OSスケーリングを調整して、画面上の一辺が実測7.12 mmであることを確認します。**PNGの80 px／マスは、物理寸法7.12 mmを保証しません。** 表示倍率は校正中に変えないでください。画像を自動点滅させる処理はありません。

機器と盤面の準備後、次を実行します。

```powershell
.\venv\Scripts\python.exe .\eventcam_workflow.py all --run sample01
```

処理順序:

1. チェッカーボードを生成、または既存の同一画像を確認する。
2. 左眼の校正画像を対話取得し、左単眼内部校正を計算する。
3. 右眼も同様に取得・内部校正する。
4. 左右の同期校正画像を30姿勢取得し、ステレオ外部校正を計算する。
5. 校正レポートを確認し、チェッカーボードを測定対象へ入れ替えるために一時停止する。Enterで続行、Qで撮影前に終了する。
6. 左右プレビューを確認して同期イベントを撮影する。
7. 左右2D粒子抽出、同期時刻の対応付け、3D三角測量、軌跡図の作成を行う。

校正は複数姿勢の準備と目視確認を必要とするため、`all`も無人自動校正ではありません。途中のコマンドが失敗すると後続工程を止めます。既存の出力を上書きする計画は、カメラ起動前に拒否します。

## 4. 工程を分けて実行する

通常はこの方法で各結果を確認すると、失敗箇所を特定しやすくなります。

### 4-1. 左右単眼校正

```powershell
.\venv\Scripts\python.exe .\eventcam_workflow.py capture-left
.\venv\Scripts\python.exe .\eventcam_workflow.py calibrate-left
.\venv\Scripts\python.exe .\eventcam_workflow.py capture-right
.\venv\Scripts\python.exe .\eventcam_workflow.py calibrate-right
```

撮影画面ではEnterで1姿勢を取得、QまたはEscでその眼の取得を終了します。各眼で30～50程度の異なる位置・距離・roll/pitch/yawを準備し、中央だけでなく四隅も覆います。計算上は10枚以上の検出成功画像が必要です。左右単眼撮影は同一瞬間である必要はありません。

LCDのリフレッシュ等で十分なイベントが出る場合は盤面を静止表示します。検出できない場合は照明・表示・姿勢を調整し、小さな盤面移動を検討します。完全に静止した物体からはイベントが出ないことがあり、通常のRGBカメラの静止画撮影とは異なります。

`calibration/left_debug`・`right_debug`のコーナー描画、`left_intrinsics.csv`・`right_intrinsics.csv`の姿勢ごとの誤差を確認します。RMSが小さいだけで、画角全体の校正精度が確保できたとは限りません。

### 4-2. ステレオ校正

```powershell
.\venv\Scripts\python.exe .\eventcam_workflow.py capture-stereo
.\venv\Scripts\python.exe .\eventcam_workflow.py calibrate-stereo
```

左右Master/Slave同期の**同一時刻窓**から両眼でコーナーを検出できた姿勢だけを採用します。左右順次撮影をステレオ対応画像として使いません。各姿勢のプレビューで操作案内に従い、両眼に盤面全体が見えることを確認します。

`calibration/stereo_debug`と `stereo_calibration.csv`を確認します。採用ペア数、再投影・epipolar誤差、基線長が実際のカメラ配置と矛盾しないことを確認してください。明らかな外れ姿勢は `exclude_pairs` に `pose_012,pose_027` の形式で記載できます。元画像を削除せず、再計算結果は別の保存先へ出します。

左右の固定、レンズ、フォーカス、解像度、相対姿勢が変わった場合は新しいセッションで校正します。

### 4-3. 同期撮影のみ

```powershell
.\venv\Scripts\python.exe .\eventcam_workflow.py record --run trial01
```

プレビューウィンドウで `Enter` を押して受理すると、カメラを開き直してハード同期を確立し、ターミナルで `Enter` を待ちます。**このターミナルの `Enter` が録画開始**です。`record_duration_sec` が `0` なら2回目の `Enter` で停止します。左右カメラの同期モード、同じ保存時間区間の完走、イベント数上限未到達を検査します。各runへ校正NPZのコピー、ハッシュ、撮影・後処理設定を保存します。記録に失敗したrunを成功扱いで解析へ回しません。

このキットは対象物や外部機器の運動・照明を制御しません。測定対象の準備と運動は操作者が行います。

### 4-4. 保存後の粒子抽出・3D復元

```powershell
.\venv\Scripts\python.exe .\eventcam_workflow.py process --run trial01
```

撮影に続けて解析する場合は次を使用します。

```powershell
.\venv\Scripts\python.exe .\eventcam_workflow.py record-process --run sample02
```

後処理はカメラへ接続しません。長い記録ではメモリとCPU時間を使うため、スパコンで処理する場合は `record` で止めて転送します。

中断した後処理を同じ設定で再開する場合:

```powershell
.\venv\Scripts\python.exe .\eventcam_workflow.py process --run trial01 --resume
```

設定または校正のハッシュが異なる再開は拒否します。同一条件で再利用できる片眼の2D結果は再利用し、3D・描画工程は再実行します。完成済みrun全体を無条件にスキップする指定ではありません。

## 5. 出力構造

```text
eventcam_standalone/
  eventcam_workflow.py
  workflow_config.json
  README_JP.md
  SETUP_OPENEB_JP.md
  requirements.txt
  checkerboard_10x7_normal.png           board実行時に生成
  venv/                                利用先で作成
  openeb_install/                       ローカルビルド時のSDK配置先
  data/session_cal20260805/
    mono_left/calib_images/
    mono_right/calib_images/
    stereo_poses/
      left/calib_images/
      right/calib_images/
      raw/                             同期校正画像の元イベント
    calibration/
      left_intrinsics.npz / .csv      キット内で校正した場合
      right_intrinsics.npz / .csv     キット内で校正した場合
      stereo_calibration.npz / .csv
      IMPORTED_CALIBRATION.txt        持込み校正の出所（同梱セッションのみ）
      left_debug/ right_debug/ stereo_debug/
    recordings/trial01/
      stereo_recording_manifest.json
      recording_config.json            取得時の設定・校正ハッシュ
      calibration/stereo_calibration.npz
      left/left_events.npz
      right/right_events.npz
      left/event_tracking/             粒子抽出後
        event_centres_raw.csv
        event_centres_interp.csv
        event_tracking_summary.json
      right/event_tracking/            左と同じ種類の成果物
      stereo_3d/
        stereo_3d_points.npz
        stereo_3d_points.csv
        stereo_3d_points_summary.json
        stereo_3d_trajectory.png
        processing_manifest.json
      standalone_processing.json       後処理状態・再開条件
```

単眼校正撮影のRAW等も取得フォルダーに保持します。通常の同期撮影はNPZを保存します。

`standalone_processing.json`の `status=complete` は処理の完走を示します。左右の抽出有効率・欠落・飛び、3D有効率・再投影誤差、図を別途確認してください。既定抽出法はイベント量の多い連結領域から1個の粒子中心を求めるもので、複数粒子のID追跡には対応していません。

出力座標はOpenCVの左カメラ座標です。概ねX右、Y下、Z前方で、長さの単位は校正盤の実寸に由来するmmです。PAT原点や実験室原点の座標ではありません。

## 6. 再実行・保存データの保護

共通入口は既存の校正出力、取得フォルダー、同名runを再利用・上書きしません。校正途中で止まった場合は、完了している工程の次から個別コマンドで再開します。部分的な撮影フォルダーを共通入口で継続する機能はありません。

追加の校正姿勢が必要な場合、同梱の低水準撮影スクリプトは既存pose番号に続けて取得できます。直接使う場合は、その `--help` を確認し、serial・盤面・倍率・同期設定を維持してください。ステレオ撮影側は取得manifestの条件一致も検査します。別実験の画像を混ぜないでください。

外れ姿勢を除いてステレオ校正を再計算する例です。以下の `stereo_calibration_reviewed.npz` とdebugディレクトリは未使用の名前にします。

```powershell
.\venv\Scripts\python.exe .\stereo_camera_calibrate.py `
  --left-images .\data\session_cal20260805\stereo_poses\left\calib_images `
  --right-images .\data\session_cal20260805\stereo_poses\right\calib_images `
  --left-intrinsics .\data\session_cal20260805\calibration\left_intrinsics.npz `
  --right-intrinsics .\data\session_cal20260805\calibration\right_intrinsics.npz `
  --left-serial 00000508 --right-serial 00000509 `
  --square-mm 7.12 --require-square-mm 7.12 --min-pairs 20 `
  --exclude-pairs pose_012,pose_027 `
  --output .\data\session_cal20260805\calibration\stereo_calibration_reviewed.npz `
  --debug-dir .\data\session_cal20260805\calibration\stereo_debug_reviewed
```

低水準スクリプトは同じ出力名を指定すると上書きし得ます。再解析では既存データを保持したコピーに対して、明示した別出力先を使ってください。共通入口が撮影に使う校正名は `calibration/stereo_calibration.npz` です。見直した校正で次の実験を行う場合は、新しいセッションのその場所へ校正を配置し、元校正は残します。

## 6-1. 単眼だけで使う

ステレオ校正も同期も使わず、カメラ1台で撮影・粒子抽出・重畳動画まで行うサンプルを同梱しています。
出力は画像座標のpxで、奥行きはありません。

```powershell
.\venv\Scripts\python.exe .\mono_sample.py all --run demo01 --serial 00000508
```

保存先は `mono_records/<run>/` で、ステレオ側の `data/<session>/` とは分かれています。
詳細は [MONO_SAMPLE_JP.md](MONO_SAMPLE_JP.md) を参照してください。

## 7. 別PC・スパコンへ移す

このディレクトリのコード・設定・指示書と対象run一式をコピーします。親プロジェクトは不要です。`workflow_config.json` の `session_dir` とrun名の相対配置を維持し、対象撮影の `recording_config.json` 内の `config` に合わせた設定ファイルを用意してください。

共通入口はrun内部の `calibration/stereo_calibration.npz` を明示して後処理するため、計測manifestに残る旧Windows絶対パスを実行時の入力として使いません。元manifestをLinuxパスへ書き換える必要はありません。

LinuxでNPZ解析だけ行う場合、OpenEB・カメラドライバは不要です。Python 3.11環境の例:

```bash
python3.11 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
MPLBACKEND=Agg ./venv/bin/python eventcam_workflow.py process --run trial01
```

Windowsのvenvは移植せず、利用先で作り直します。ジョブ投入・メモリ・時間制限は利用施設に合わせます。同じrunを複数ジョブで同時処理しないでください。設定を変える解析は、別のrunコピーに対して実施します。

## 8. 同梱ファイルと検証範囲

| ファイル | 役割 |
|---|---|
| `eventcam_workflow.py` | 設定検査、工程の連結、dry-run、校正・run保護 |
| `check_environment.py` / `activate_metavision_env.ps1` | 依存ライブラリ確認／Windowsの環境変数設定 |
| `eventcam_checkerboard_calibration_capture.py` | 単眼校正画像の取得 |
| `single_camera_calibrate.py` | 各眼の内部校正 |
| `stereo_checkerboard_sync_capture.py` | 同一時刻窓のステレオ校正画像取得 |
| `stereo_camera_calibrate.py` | 固定内部パラメータによる左右外部校正 |
| `stereo_eventcam_record_sync.py` | 左Master／右Slaveの同期撮影 |
| `eventcam_scale_calibration_capture.py` | SDK読込み、カメラを開く共通関数 |
| `eventcam_npz_storage.py` | NPZ保存 |
| `stereo_process_recording.py` | 左右粒子抽出と3D復元の処理順序 |
| `eventcam_npz_track.py` | 片眼イベントから粒子中心を抽出 |
| `stereo_triangulate_tracks.py` | 左右2D中心から三角測量 |
| `stereo_plot_3d_points.py` | 3D軌跡図 |
| `eventcam_npz_render_video.py` | 任意のイベント／追跡重畳動画 |
| `mono_sample.py` | 単眼サンプルの共通入口（devices/record/track/video/all） |
| `mono_eventcam_record.py` | カメラ1台の撮影とNPZ書き出し |
| `RECORDING_RUNBOOK_JP.md` | 校正済みからの撮影〜粒子抽出の実行手順 |
| `MONO_SAMPLE_JP.md` | 単眼での撮影・粒子抽出・動画化のサンプル手順 |
| `SOURCE_SNAPSHOT.json` | コピー元と同梱版コードのハッシュ |
| `selftest.py` | 人工データを使う実機不要の独立動作テスト |

元の校正・同期・抽出処理をコピーし、独立用に校正パスを必須引数化、SDK探索先をこのディレクトリまたは明示環境変数に変更しました。`no-preview`や同期OFF等を持つ低水準入口もありますが、共通入口では左Master同期と撮影前プレビューを使います。

同梱版は親ディレクトリから切り離したコピーでヘルプ・dry-run・人工イベントの左右粒子抽出と3D復元を検証します。実機での校正・撮影、クリーン環境へのOpenEB新規ビルドは、このパッケージ作成時には実行していません。検証結果は [VALIDATION_JP.md](VALIDATION_JP.md) を参照してください。

別PCへ移した後にも、このフォルダー内だけで検証できます。人工データはOSの一時ディレクトリに作成し、既存の計測データには触れません。

```powershell
.\venv\Scripts\python.exe -B .\selftest.py -v
```

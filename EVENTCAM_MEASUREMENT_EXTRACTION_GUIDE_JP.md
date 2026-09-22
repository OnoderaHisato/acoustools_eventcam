# イベントカメラ計測・粒子抽出のファイルと手順

作成日: 2026-09-16。対象は、このプロジェクトの現行AcousTools/PAT＋左右ステレオイベントカメラによる計測です。

基本の流れは **計測 → 保存・撮影範囲の確認 → 左右2D粒子抽出 → ステレオ3D復元 → 理想軌道比較** です。計測と重い後処理は分離します。未処理データはスパコンへ移して解析する現行方針に従います。

この資料を作るために実機の起動、計測、粒子抽出は実行していません。以下の実機・後処理コマンドは、実際にその工程を行う際の実行例です。

## 1. 通常使う入口

| 用途 | スクリプト | 入力・処理内容 |
|---|---|---|
| 対話式の計測 | `acoustools_stereo_eventcam_3d_recording.py` | 軌道を選んでPATを動かし、左右イベントと同期情報を保存。粒子抽出はしない |
| JSON／事前生成軌道からの連続計測 | `acoustools_stereo_eventcam_3d_recording_auto.py` | `--json` または `--hf-export-dir` で条件を指定。計測だけを行う |
| 撮影範囲の軽量確認 | `stereo_acoustools_3d_quick_preview.py` | 保存済みイベントから低fpsの左右結合動画・一覧画像を作成 |
| 通常の後処理 | `stereo_acoustools_3d_postprocess.py` | runディレクトリを受け取り、左右2D抽出→3D復元→理想軌道比較を実行 |
| 未処理runの一括処理 | `stereo_acoustools_3d_auto_monitor.py` | 記録ルート内の完成済み・未処理runを検索して処理 |
| 単眼の2D粒子抽出だけ | `eventcam_npz_track.py` | 片側のイベントNPZから粒子中心の時系列を抽出 |

`acoustools_stereo_eventcam_3d_pipeline.py` は計測と後処理を一度に行う入口です。現在の分離運用では、上表のrecordingとpostprocessを使います。

## 2. 一緒に必要なスクリプト

入口だけをコピーすると動作しません。参照先のスクリプトも同じディレクトリに置きます。

### 計測側

| ファイル | 役割 |
|---|---|
| `stereo_acoustools_3d_recording_core.py` | PAT保持・軌道実行・左右記録・LED同期・manifest保存 |
| `stereo_acoustools_3d_common.py` | 共通設定、校正検査、manifest処理 |
| `stereo_eventcam_record_sync.py` | 左Master／右Slaveの同期記録と共通保存区間の管理 |
| `eventcam_scale_calibration_capture.py` | カメラSDKの読み込み、カメラ接続などの共通関数 |
| `eventcam_npz_storage.py` | イベントNPZの保存 |
| `stereo_detect_pat_start_led.py` | LEDイベントからPAT開始時刻を検出 |
| `acoustools_eventcam_sync.py` | PAT送信・ホログラム生成などの共通処理 |
| `acoustools_multitraj_no_eventcam.py` | 基本軌道の生成・統計・プレビュー |
| `acoustools_random3d_no_eventcam.py` | ランダム／拡張3D軌道などの生成 |
| `acoustools_cusped3d.py` | カスプ軌道の生成 |
| `stereo_acoustools_3d_auto_common.py` | 自動計測のJSON読込・条件選択・プレビュー |
| `stereo_acoustools_3d_hf_common.py` | HF／ステップ応答／Plan Bのexport読込と条件検査 |
| `hf_identification_trajectory.py` | HF等の指令生成・export検証 |

最後の3本は自動計測入口で参照します。ファイル名に `no_eventcam` や `calibration_capture` が含まれていても、現行計測が共通関数として利用するため必要です。個別に実行する必要はありません。

### 後処理側

通常の「2D抽出＋3D復元＋理想比較」に必要なPythonファイルは次の8本です。

| ファイル | 役割 |
|---|---|
| `stereo_acoustools_3d_postprocess.py` | 後処理の実行入口、複数run処理、中断再開 |
| `stereo_acoustools_3d_postprocess_core.py` | 計測時設定の復元と処理順序の管理 |
| `stereo_acoustools_3d_common.py` | 共通設定、校正・manifest処理 |
| `stereo_process_recording.py` | 左右の2D抽出、同期時刻の対応付け、3D復元の呼出し |
| `eventcam_npz_track.py` | 各カメラの粒子中心抽出 |
| `stereo_triangulate_tracks.py` | 左右の2D中心と校正から3D三角測量 |
| `stereo_plot_3d_points.py` | 3D軌跡の図を作成 |
| `stereo_compare_ideal_3d.py` | 計測3D軌道とPAT理想軌道の比較 |

追跡点を重ねた動画を作る場合は `eventcam_npz_render_video.py` も必要です。低fpsの撮影範囲確認には、別途 `stereo_acoustools_3d_quick_preview.py` を使います。

通常のNPZ後処理はNumPy・OpenCV・MatplotlibとPython標準ライブラリで動作する構成で、カメラSDKやPATへの接続を必要としません。一方、`stereo_acoustools_3d_auto_monitor.py` は現状、自動計測の共通モジュールを通じて計測側のコードもimportします。解析専用環境では、依存関係が少ない `stereo_acoustools_3d_postprocess.py` にrun一覧を渡す構成が適しています。

## 3. 入力データ・実行環境

| 項目 | 現行設定／必要なもの |
|---|---|
| Windows上のPython | プロジェクト直下の `.\venv\Scripts\python.exe` |
| 計測環境 | AcousTools、PyTorch、OpenMPDのライブラリ、OpenEB／Metavision、SilkyEvCam用ドライバ・プラグイン、NumPy、OpenCV、Matplotlib等 |
| 左カメラ | `00000508`、Master |
| 右カメラ | `00000509`、Slave |
| 同期 | 左右Master／Slaveのハードウェア同期 |
| PAT開始LED | 左カメラ、ROI `600,0,1280,180` |
| ステレオ校正 | `stereo_checkerboard_calib_extrinsics_20260805/stereo_calibration_square7p12_extrinsics_final.npz` |
| JSON自動計測の入力 | 実行する軌道JSON。include型JSONでは参照先JSONも必要 |
| HF等の入力 | 指定したexportディレクトリ一式。NPZ指令・metadata等を含める |
| PAT座標で絶対誤差を評価する場合 | 同じステレオ校正に対応する `camera_to_pat_transform.npz` |

初期ランダム計画は `long_random_3d_patterns_initial.json`。95／90／85／80%版はそれぞれ `long_random_3d_patterns_initial_scale95.json`、`...scale90.json`、`...scale85.json`、`...scale80.json` です。実験で指定されたファイルをそのまま使い、縮尺を自動で選び替えないでください。

GitHubには計測データ、校正NPZ、軌道export、SDK、venv等は含まれません。コードのcloneだけでは実験環境は復元されません。環境構築の詳細は [WINDOWS_EVENTCAM_SETUP_v2.md](WINDOWS_EVENTCAM_SETUP_v2.md) を参照してください。

## 4. 計測手順

コマンド例はプロジェクト直下のPowerShellで実行します。`<使用する軌道JSON>` と `<RUN>` は実際のパスへ置き換えます。

### 4-1. 軌道・条件を実機なしで確認する

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json ".\<使用する軌道JSON>" --dry-run
```

軌道PNGも作成する場合は `--dry-run` の代わりに `--preview-only` を使います。対象条件、振幅、時間、PAT更新レート、速度・加速度、画角との対応を確認します。dry-runの成功だけで粒子保持が保証されるわけではありません。

### 4-2. 計測する

対話式で1本だけ計測する場合:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording.py --single-run
```

JSONから最初の1条件を計測する場合:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json ".\<使用する軌道JSON>" `
  --limit 1 --confirm-each-run --preview-each-run
```

**これらはPATを出力し、粒子を動かし、左右カメラを開く実機コマンドです。** 左右の同期配線、粒子の両眼可視、LED位置、対象軌道を確認して実行します。

プログラム内では、おおむね次の順で処理します。

1. PATで粒子を中心保持し、左右プレビューを確認する。
2. 軌道のホログラムを準備し、粒子を開始点へ移す。
3. 左右カメラをMaster／Slave同期で記録する。
4. 共通保存区間の開始通知を受けてPAT軌道を送信する。
5. 左右イベントを保存し、左LEDからPAT開始時刻を確定する。
6. 成功runのmanifestを保存し、粒子を中心へ戻す。

通常の対話式入口は `--single-run` を外すと複数軌道を続けて選択できます。autoも複数条件を選択できますが、HF・ステップ応答・Plan B・保持限界条件には専用の確認フラグや実行順序があります。末尾の各専用手順書に従ってください。

複数runの同一セッション中はPAT保持を継続し、セッションの終了処理で停止します。LED検出失敗は成功扱いにせず再取得を確認し、失敗試行データは既定で削除します。`--keep-failed-captures` は失敗原因を調査する場合だけ使います。既存の成功計測成果物は保持します。

### 4-3. 保存結果を確認する

対話式の既定保存先は `stereo_acoustools_3d_records/`、autoは `stereo_acoustools_3d_records_auto/` です。

- `pipeline_manifest.json`: `capture_complete=true`、通常は `processing_status=pending`。
- 左右の `left_events.npz`／`right_events.npz` が存在する。
- `stereo_recording_manifest.json` 等で左右同期、共通記録区間、保存成功、イベント数上限への未到達を確認する。
- `pat_camera_timing.json` と `pat_start_led/` でLED検出とPAT運動区間が記録内に収まることを確認する。
- 左右の粒子像が撮影範囲から外れていないことを確認する。

保存済み1 runの軽量動画を作成する例:

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_quick_preview.py `
  ".\stereo_acoustools_3d_records_auto\<RUN>"
```

既定は5 fps、50 ms積算の左右結合動画です。粒子抽出や3D復元は行わず、`processing_status` も変更しません。低fpsの動画は撮影範囲の確認用で、高速応答や全時刻の追跡品質の評価には使えません。

## 5. 粒子抽出・3D復元の手順

重い工程です。以下は後処理を行うと決めた環境で実行します。現在の未処理データはスパコン処理を基本とし、ローカルで一括処理を始めないでください。

### 5-1. 通常はrun全体を専用入口へ渡す

Windows環境でのコマンド形式:

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_postprocess.py `
  ".\stereo_acoustools_3d_records_auto\<RUN>"
```

渡すのは **`pipeline_manifest.json` があるrunディレクトリ** です。内部の `stereo_recording` ディレクトリやNPZ単体を渡す入口ではありません。

計測時の `processing_config`、左LEDマスク、同期時刻を読み戻し、次を実行します。

1. 左右それぞれのイベントを短時間窓で積算し、連結したイベント領域から粒子中心を抽出する。既定はイベント数で重み付けした重心 `event_weighted`。
2. 欠落時間と位置ジャンプの制限を適用し、一定間隔の左右2D軌道CSVを作る。
3. 左右の時刻を対応付け、校正NPZを使って3D三角測量する。再投影誤差を検査する。
4. LEDで得たPAT開始時刻に合わせてideal logと比較し、誤差と図を出力する。

| 主な処理条件 | 共通既定値 |
|---|---|
| 積算窓 `window_us` | 200 µs |
| 推定間隔 `hop_us` | 100 µs |
| 補間間隔 `dt_us` | 100 µs |
| 最大補間欠落時間 | 5 ms |
| 最大2D移動量 | 1ステップ15 px |
| 粒子追跡ROI | `0,0,1280,720` |
| LED除外 | 左だけ `600,0,1280,180` |
| 最大再投影誤差 | 3 px |

実際のrunではmanifestに保存した条件が優先されます。上表の粒子追跡ROIとLED ROIは別の設定です。LED領域を右画像からも除外する処理にはしません。

中断後に同じ条件で再開する場合:

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_postprocess.py `
  "<RUN_1>" "<RUN_2>" --resume --keep-going
```

`--resume` は完成済みrunをスキップし、入力・条件・成果物を検証できた片眼の追跡結果を再利用します。`--keep-going` は一つの後処理が失敗しても次のrunへ進む指定です。再開条件が一致しない場合は再計算され得ます。

条件変更の比較は元runを保全した解析用コピーで行い、`--resume` は使いません。後処理はrun内のmanifestと解析結果を書き込むため、既存結果を残したい場合は同じ出力先で再実行しないでください。

計測環境で未処理runを一括処理する入口もあります:

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_auto_monitor.py `
  .\stereo_acoustools_3d_records_auto --once
```

これは監視・一覧表示だけでなく、実際に重い後処理を開始します。計測中の通常運用では起動しません。`--new-only` は起動後に作られたrunだけを対象とするため、既存データを処理する用途には付けません。

### 5-2. 単眼2D抽出だけを行う場合

`eventcam_npz_track.py` には片側のイベントNPZを渡します。次は共通既定値に合わせた左眼の例です。実runに別の処理条件が保存されている場合は、その値を使います。

```powershell
.\venv\Scripts\python.exe .\eventcam_npz_track.py `
  ".\stereo_acoustools_3d_records_auto\<RUN>\stereo_recording\left\left_events.npz" `
  --output-dir ".\analysis_2d_new\<RUN>\left" `
  --window-us 200 --hop-us 100 --dt-us 100 `
  --max-interp-gap-sec 0.005 --max-step-px 15 `
  --roi 0,0,1280,720 --mask-roi 600,0,1280,180 `
  --tracking-method event_weighted
```

右眼は入力を `right\right_events.npz`、出力を別のright用ディレクトリへ変更し、左LED用の `--mask-roi` を外します。既存結果を上書きしない未使用の出力先を指定してください。

この単独入口はpipeline manifestの条件を自動復元せず、3D復元や理想軌道比較も実行しません。単独入口の無指定時の窓幅等は共通後処理と異なるので、上記のように条件を明示します。

## 6. 保存される成果物

```text
<RUN>/
  pipeline_manifest.json              計測・後処理条件、完了状態
  pat_camera_timing.json              PATとカメラの時刻対応
  capture_start_marker.json           共通保存区間の開始通知
  *_ideal_log.csv                     PAT指令軌道
  *_trajectory_preview.png            指令軌道のプレビュー
  pat_start_led/                      LED同期検出の診断
  stereo_recording/
    stereo_recording_manifest.json    左右カメラの記録情報
    left/
      left_events.npz                左イベント
      event_tracking/                後処理後に生成
        event_centres_raw.csv         各窓の抽出中心・有効判定
        event_centres_interp.csv      等間隔に補間した2D軌道
        event_tracking_summary.json   追跡設定・抽出状況
    right/
      right_events.npz               右イベント
      event_tracking/                左と同種の追跡結果
    stereo_3d/                       後処理後に生成
      stereo_3d_points.csv
      stereo_3d_points.npz
      stereo_3d_points_summary.json
      stereo_3d_trajectory.png
      processing_manifest.json
  ideal_comparison_3d/               後処理後に生成
    stereo_ideal_comparison.csv
    stereo_ideal_comparison.npz
    stereo_ideal_comparison_summary.json
    ideal_comparison_*.png
```

HF等のrunには `command_trajectory.npz`、`trajectory_metadata.json`、`command_offset_log.csv` 等も保存されます。RAWやcapture metadataがある場合も、そのまま保持します。

後処理完了時は `processing_status=complete` と最終成果物の存在を確認します。さらに、左右のraw追跡有効率・欠落・跳び、3D有効点率・再投影誤差、理想軌道との比較図を確認します。処理が最後まで終了したことと計測品質の合格は分けて判断します。

ステレオ3D点は左カメラ座標系です。独立に取得した `camera_to_pat_transform.npz` を指定すればPAT座標で絶対位置比較できます。指定しない場合はrunごとの回転・並進fitによる軌道形状比較となり、絶対位置精度の評価にはなりません。

## 7. スパコン・別PCへ渡すものと移設時の注意

1. 後処理側の8本のPythonファイル。必要に応じて動画用スクリプトも追加する。
2. 解析する **runディレクトリ一式**。左右NPZだけでなく、manifest、timing、ideal log、同期・LED情報、HF指令等も含める。
3. 計測に用いたステレオ校正NPZ。絶対比較をする場合は対応するcamera-to-PAT変換NPZも含める。
4. 対応する計画JSON／export、auto session JSON、実験条件の記録。コードの版と転送ファイルのサイズ・ハッシュも記録すると照合できる。

Linuxの解析環境には、別途PythonとNumPy・OpenCV・Matplotlibを用意します。Windowsの `venv` をそのままコピーして実行する構成ではありません。ジョブ投入方式は利用するスパコンの規則に合わせます。

**転送しただけでは、保存済みWindowsパスが解決しない場合があります。** 現行の `stereo_acoustools_3d_postprocess.py` は校正を `pipeline_manifest.json` の `stereo_calibration` から読み、校正を上書きする `--stereo-calibration` オプションは持っていません。

移設後は元データを保持し、解析用コピーのmanifestで次の参照先を転送先に合わせます。数値データ、カメラserial、同期方式、LED設定、校正の内容は変更しません。

| manifestの項目 | 転送先で確認する内容 |
|---|---|
| `stereo_calibration` | コピーした同一校正NPZへの有効なパス |
| `ideal_log` | 当該runのideal logへの有効なパス |
| `camera_to_pat_transform` | 使用する場合のみ、同一変換NPZへの有効なパス。後処理CLIからの指定も可能 |

ideal logには同名ファイルをrun直下から探す補助処理がありますが、LinuxではWindowsのバックスラッシュ区切りをそのまま解釈できません。OSをまたぐ移設ではこの補助処理に依存せず、上の参照先を明示的に整えます。校正・変換NPZは元と同一内容であることをハッシュ等で確認します。

NPZ後処理は実機へ接続しませんが、メモリと処理時間を使います。最初の1 runで環境・出力・資源量を確認してから処理対象を増やし、同じrunへ複数ジョブを同時に書き込まないようにします。今回の資料作成では転送・manifest修正・解析は行っていません。

## 8. 詳細手順書

| 目的 | 手順書 |
|---|---|
| 通常の計測・同期・後処理 | [STEREO_ACOUSTOOLS_3D_PIPELINE_JP.md](STEREO_ACOUSTOOLS_3D_PIPELINE_JP.md) |
| JSON自動計測、軽量動画、モニター | [STEREO_ACOUSTOOLS_3D_AUTO_JP.md](STEREO_ACOUSTOOLS_3D_AUTO_JP.md) |
| 追加3D軌道の統合計画 | [ALL_ADDITIONAL_3D_DATASET_JP.md](ALL_ADDITIONAL_3D_DATASET_JP.md) |
| HF同定 | [HIGH_FREQUENCY_IDENTIFICATION_HANDOFF.md](HIGH_FREQUENCY_IDENTIFICATION_HANDOFF.md) |
| ステップ応答 | [STEP_RESPONSE_MEASUREMENT_JP.md](STEP_RESPONSE_MEASUREMENT_JP.md) |
| Plan B追加計測 | [PLAN_B_ADDITIONAL_MEASUREMENT_JP.md](PLAN_B_ADDITIONAL_MEASUREMENT_JP.md) |
| Windows環境構築 | [WINDOWS_EVENTCAM_SETUP_v2.md](WINDOWS_EVENTCAM_SETUP_v2.md) |
| 実験の経緯・現行方針 | [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md) |

過去の `STEREO_MEASUREMENT_WORKFLOW_CURRENT.md` は履歴資料との注記があるため、現行コマンドは上記3D手順書と現在のコードを優先してください。

# AcousTools・ステレオイベントカメラ 3D軌道計測

## 実行入口

計測だけを行い、重い粒子抽出を後回しにする通常の入口:

```powershell
.\venv\Scripts\python.exe acoustools_stereo_eventcam_3d_recording.py
```

このスクリプトは対話型の複数軌道ループです。1本の計測が完了するたびに、別の軌道を
続けて計測するか確認します。続行すると、次の18種類から改めて選び、振幅・周波数・
反復数などを入力できます。AcousTools/OpenMPD接続はループ全体で1回だけ開き、PAT出力は
条件間も保持します。左右カメラは各ランで開き直してMaster/Slave同期を検証し、結果は
ランごとの別ディレクトリへ保存します。

```text
 1: Z-axis vibration
 2: Elliptical orbit (XZ plane)
 3: Diagonal line (XZ plane)
 4: Heart-shaped orbit (XZ plane)
 5: Rectangular orbit (XZ plane)
 6: Vertical figure-eight (XZ plane)
 7: Horizontal infinity (XZ plane)
 8: Single slanted line (XZ plane)
 9: S-shaped orbit (XZ plane)
10: X-axis vibration
11: Long random 3D stroke
12: 3D figure-eight
13: Closed toroidal helix
14: Trefoil knot
15: 3D Lissajous
16: Extended multi-band random 3D excitation
17: Three-axis chirp excitation
18: Cusped planar/lifted-3D curve
```

mode 8は開軌道です。指定した反復時間中に終点から始点へ飛ばず、1回の斜線移動後は
終点を保持します。それ以外のパラメトリック軌道は閉軌道として指定回数を反復します。

従来どおり1本だけ計測して終了する場合は次を使用します。

```powershell
.\venv\Scripts\python.exe acoustools_stereo_eventcam_3d_recording.py --single-run
```

いずれも計測・同期・左右NPZ保存までで終了し、重い粒子抽出は実行しません。
計測完了後、表示されたコマンド、または次の専用スクリプトを別途実行します。

```powershell
.\venv\Scripts\python.exe stereo_acoustools_3d_postprocess.py `
  stereo_acoustools_3d_records\<RUN>
```

後処理はカメラやPATへ接続せず、左右2D粒子抽出、3D三角測量、ideal log比較を順に
実行します。複数ランを空白区切りで指定すると、順番に処理できます。

計測から後処理までを一度に実行する全工程入口:

```powershell
.\venv\Scripts\python.exe acoustools_stereo_eventcam_3d_pipeline.py
```

このpipelineはrecording coreを実行し、PATとカメラの終了処理が完了してからpostprocess coreを
実行します。

PAT開始LEDを左カメラだけで検出する場合:

```powershell
.\venv\Scripts\python.exe acoustools_stereo_eventcam_3d_recording.py `
  --pat-start-led-side left `
  --pat-start-led-roi 600,0,1280,180
```

右カメラだけにLEDを置く場合は `--pat-start-led-side right` にします。LEDが反対側の
カメラに映っている必要はありません。左右カメラはMaster/Slave同期で共通の時刻領域を
使うため、片側で検出したLEDイベント時刻を左右両方の粒子軌道へ適用できます。

指定したLED ROIは検出側の2D粒子追跡からだけ除外されます。反対側のイベントには
マスクを適用しません。その後の3D抽出は従来どおり左右両方の粒子中心を使います。
ROIは端へ寄せ、粒子の全移動範囲と重ならないようにしてください。

イベント数閾値は既定で自動決定します。明示する場合は、例えば次を追加します。

```powershell
--pat-start-led-bin-us 100 --pat-start-led-threshold 30
```

LEDを検出できなかった試行は、その場で成功扱いにせず、LEDを確認して同じ軌道を
再計測するか確認します。`Y` または Enter で再計測します。再計測時は計算済みの
ホログラムを再利用し、粒子を軌道開始点へ戻してから左右カメラを新しく開き直します。

失敗試行の左右RAW/NPZ、開始マーカー、LED診断は既定では直ちに完全削除されます。
再計測しない場合は、その失敗ランのideal logとプレビューも削除します。LED調整用に
失敗データを調査したい場合だけ、次を追加してください。

```powershell
--keep-failed-captures
```

この場合、失敗試行はランディレクトリ内の `_capture_attempt_XX` に残ります。成功した
試行だけが通常の `stereo_recording` と `pat_start_led` へ移され、後続の3D処理に使われます。

既定値では次を使用します。

- 左カメラ: `00000508`（Master）
- 右カメラ: `00000509`（Slave）
- ステレオ校正:
  `stereo_checkerboard_calib_extrinsics_20260805/stereo_calibration_square7p12_extrinsics_final.npz`
- 左右追跡窓: 200 us
- 追跡hop・補間間隔: 100 us
- 三角測量の最大再投影誤差: 3 px
- PAT開始LED: 左カメラの右上端 `600,0,1280,180`
- LED探索半幅の基本余白: 200 ms
- 実際の探索半幅: `200 ms + max(0, send_message実時間 - ideal軌道時間)`
- カメラ記録の軌道末尾余裕: 2.0 s（PATメッセージ転送開始遅延を含む）
- 2D補間する最大欠落時間: 5 ms
- 2D中心の最大1ステップ移動: 15 px

計測スクリプトの処理順序は次のとおりです。

1. PATを原点で浮揚させる。
2. 左右カメラのプレビューで粒子が両方に見えることを確認する。
3. ランダム3Dまたは既存の閉3D軌道を選択する。
4. 全ホログラムを計算し、粒子を軌道開始点へ移動する。
5. 左右カメラをMaster/Slaveハード同期で開く。
6. 共通カメラ時刻の保存区間が開始した通知を受けてPAT軌道を開始する。
7. 左右イベントをNPZへ保存する。
8. 同期時刻と後処理条件をmanifestへ保存する。
9. 粒子を中央へ戻し、後処理を開始せず終了する。

後処理スクリプトは次を行います。

1. 左右それぞれから2D粒子中心を抽出する。
2. 校正NPZで3D三角測量し、左右再投影誤差を検査する。
3. PAT開始時刻とカメラ開始時刻を対応付け、ideal logと比較する。

## 絶対比較と軌道形状比較

指定されたステレオ校正NPZは、左右カメラ間の内部・外部校正を持っていますが、
左カメラ座標からAcousTools/PAT座標への変換は持っていません。

独立に計測した `camera_to_pat_transform.npz` がある場合は、次のように指定します。

```powershell
.\venv\Scripts\python.exe acoustools_stereo_eventcam_3d_recording.py `
  --camera-to-pat-transform PAT_GRID_SESSION\registration\camera_to_pat_transform.npz
```

この場合は固定変換

```text
p_pat_mm = R_camera_to_pat @ p_left_camera_mm + t_camera_to_pat_mm
```

を使い、PAT座標における絶対位置誤差を評価します。変換作成時と今回のステレオ校正の
SHA-256が違う場合は処理を停止します。

変換を指定しない場合は、計測した軌道とideal軌道から回転・並進を剛体fitします。
この結果は軌道形状・振幅・時間追従の評価には使用できますが、カメラとPATの絶対的な
設置位置・姿勢誤差はfitによって除かれるため、絶対精度ではありません。出力JSONにも
`per-run rigid fit (shape comparison only)` と記録されます。

新しい2026-08-05校正で絶対比較する場合は、カメラやPATを動かさずに
`pat_stereo_grid_capture.py` と `pat_stereo_grid_process.py` を使って、同じ校正に対応する
camera-to-PAT登録を新しく作成してください。

## 主な成果物

`event_tracking`、`stereo_3d`、`ideal_comparison_3d` は後処理スクリプトの完了後に追加されます。

```text
stereo_acoustools_3d_records/<shape>_<timestamp>/
  pipeline_manifest.json
  pat_camera_timing.json
  capture_start_marker.json
  pat_start_led/
    left_pat_start_led.json
    left_pat_start_led_bins.csv
    left_pat_start_led_counts.png
  *_ideal_log.csv
  *_trajectory_preview.png
  stereo_recording/
    stereo_recording_manifest.json
    left/
      left_events.npz
      event_tracking/
        event_centres_raw.csv
        event_centres_interp.csv
        t_xy.png
        xy.png
    right/
      right_events.npz
      event_tracking/
        event_centres_raw.csv
        event_centres_interp.csv
        t_xy.png
        xy.png
    stereo_3d/
      stereo_3d_points.csv
      stereo_3d_points.npz
      stereo_3d_points_summary.json
      stereo_3d_trajectory.png
      processing_manifest.json
  ideal_comparison_3d/
    stereo_ideal_comparison.csv
    stereo_ideal_comparison.npz
    stereo_ideal_comparison_summary.json
    ideal_comparison_time_xyz.png
    ideal_comparison_errors.png
    ideal_comparison_trajectory_3d.png
```

`stereo_ideal_comparison_summary.json` にはX/Y/Zそれぞれと3D誤差ノルムについて、
平均、標準偏差、MAE、RMSE、95パーセンタイル絶対誤差、最大絶対誤差が入ります。

## 追跡確認動画

左右のイベントと追跡点を重ねた動画も作る場合:

```powershell
.\venv\Scripts\python.exe acoustools_stereo_eventcam_3d_recording.py --render-overlay
```

この指定はmanifestへ保存され、後から専用後処理スクリプトを実行したときに適用されます。

## 後処理だけを再実行

計測完了後の通常の後処理は次です。

```powershell
.\venv\Scripts\python.exe stereo_acoustools_3d_postprocess.py `
  stereo_acoustools_3d_records\<RUN>
```

計測時に指定した追跡条件は `pipeline_manifest.json` から自動復元されます。条件を変更して
再処理する場合は、後処理コマンドへ上書き値を直接指定します。

途中で後処理を停止した場合は `--resume` を付けて同じラン一覧を再指定できます。最終成果物
まで完成しているランはスキップされ、処理途中のランでは、入力NPZ・追跡条件・出力ファイルを
検証できた左／右カメラの2D追跡だけが再利用されます。未完成または条件が異なる側はRAWイベント
から再計算されます。

```powershell
.\venv\Scripts\python.exe stereo_acoustools_3d_postprocess.py `
  <RUN_1> <RUN_2> <RUN_3> `
  --resume --keep-going
```

処理条件を変更して再解析したい場合は `--resume` を付けないでください。`--resume` は同一条件の
中断再開用です。

```powershell
.\venv\Scripts\python.exe stereo_acoustools_3d_postprocess.py `
  stereo_acoustools_3d_records\<RUN> `
  --window-us 300 --hop-us 100 --dt-us 100 --roi 300,150,1000,650
```

## 時刻同期

左右カメラはMaster/Slaveケーブルで同じカメラ時刻領域を使用します。Masterが共通保存区間
`[start, end)` に到達すると `capture_start_marker.json` をatomicに書き、PAT側はその通知後に
軌道送信を開始します。カメラ時刻が保存区間開始からマーカー通知までに進んだ時間と、
マーカー通知からPAT送信までのPC単調時計時間を合わせて、ideal logの初期時刻を求めます。
比較時には既定でその周囲 ±20 msを100 us刻みで探索し、実際の送信・検出遅延を微調整します。

`--pat-start-led-side left/right` を指定した場合は、上記PC時刻をLED探索の概略時刻としてだけ
使います。基本探索半幅は ±200 msで、長いPATメッセージの転送に時間がかかった場合は
`send_message実時間 - ideal軌道時間` を自動加算します。その範囲にある指定側ROIの
LEDイベント立ち上がりを最終的なPAT開始時刻にします。左右それぞれでLEDを検出して
平均する処理ではありません。既定では比較側の
軌道fitによる時刻変更も行いません。LED時刻の周囲をさらに軌道fitで微調整する診断が必要な
場合だけ `--refine-led-time` を指定します。

## 実行入口とcoreの依存関係

```text
acoustools_stereo_eventcam_3d_recording.py
  -> stereo_acoustools_3d_recording_core.py
     -> stereo_eventcam_record_sync.py
     -> stereo_detect_pat_start_led.py
     -> AcousTools・3D軌道ユーティリティ

stereo_acoustools_3d_postprocess.py
  -> stereo_acoustools_3d_postprocess_core.py
     -> stereo_process_recording.py
        -> eventcam_npz_track.py（左右）
        -> stereo_triangulate_tracks.py
        -> stereo_plot_3d_points.py
        -> eventcam_npz_render_video.py（任意）
     -> stereo_compare_ideal_3d.py

acoustools_stereo_eventcam_3d_pipeline.py
  -> recording core
  -> postprocess core

recording core ─┐
                ├-> stereo_acoustools_3d_common.py
postprocess core┘
```

recording coreとpostprocess coreは互いをimportしません。両者の境界はランディレクトリ内の
`pipeline_manifest.json`、`pat_camera_timing.json`、左右イベントNPZ、ideal logです。

## JSON自動計測

複数の3Dランダム軌道をJSONから連続計測し、完成runを別プロセスで監視・後処理する構成は
`STEREO_ACOUSTOOLS_3D_AUTO_JP.md`を参照してください。自動化版も上記と同じrecording coreと
postprocess coreを使用します。

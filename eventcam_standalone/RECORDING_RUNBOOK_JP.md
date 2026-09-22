# 撮影〜粒子抽出 実行手順

作成: 2026-09-16。このキットで1回の計測を最初から最後まで通すための手順書です。
校正は済んでいる前提で、`data/session_cal20260805/calibration/stereo_calibration.npz` を使います。
校正からやり直す場合は [README_JP.md](README_JP.md) §3 を参照してください。

## 0. パスの設定

PowerShellを開いて1回だけ実行します。以降の手順はこの変数を使います。

```powershell
$KIT  = "C:\Users\digit\Documents\scripts\python\eventcam_control\eventcam_standalone"
$PY   = "C:\Users\digit\Documents\scripts\python\eventcam_control\venv\Scripts\python.exe"
$RUN  = "trial01"

cd $KIT
$env:EVENTCAM_OPENEB_ROOT = "C:\Users\digit\Documents\scripts\python\eventcam_control\openeb_install"
$env:EVENTCAM_VCPKG_BIN   = "C:\Users\digit\Documents\scripts\python\eventcam_control\vcpkg\vcpkg_installed\x64-windows\bin"
```

キット専用venvを作った場合は `$PY = "$KIT\venv\Scripts\python.exe"` とし、
環境変数2行の代わりに `. .\activate_metavision_env.ps1` を実行します。

`$RUN` は撮影ごとに変えます。同じ名前は2回使えません（既存runの上書きを拒否します）。

---

## 1. 環境確認 — カメラを開かない

```powershell
& $PY .\eventcam_workflow.py doctor --sdk
```

期待する出力:

```
[OK] numpy: ...
[OK] cv2: ...
[OK] matplotlib: ...
[OK] OpenEB event I/O, HAL, Core and UI bindings imported. Cameras were not enumerated/opened.
```

`[FAIL] OpenEB:` が出たら [SETUP_OPENEB_JP.md](SETUP_OPENEB_JP.md) へ。`EVENTCAM_OPENEB_ROOT` の綴りが最初の疑いです。

## 2. デバイス確認 — 列挙のみ

```powershell
& $PY .\eventcam_workflow.py devices
```

`00000508` と `00000509` の2台が見えることを確認します。片方しか出ない場合はUSBと電源を確認してください。
2台見えない状態で先に進むと、撮影スクリプトが `Two cameras are required.` で止まります。

## 3. 実行予定の確認 — 何も書き込まない

```powershell
& $PY .\eventcam_workflow.py record --run $RUN --dry-run
```

表示されるコマンド行で、serial・`--hw-sync left-master`・保存先・`--duration-sec` を目で確認します。
`--dry-run` はカメラSDKをimportせず、子プロセスもファイル書込みも行いません。

## 4. 撮影

配線を確認します。**左 SYNC_OUT → 右 SYNC_IN**。左がMaster、右がSlaveです。

```powershell
& $PY .\eventcam_workflow.py record --run $RUN
```

進行:

1. 校正NPZの検査結果が表示されます（serial、画像サイズ、基線長、stereo_rms）。
2. **左右プレビューウィンドウ**が開きます。ここでの `Enter` は受理であって録画開始ではありません。

   | キー | 動作 |
   |---|---|
   | `Enter` | 受理してカメラを開き直す |
   | `R` | 溜まったイベントを捨てて表示を更新 |
   | `Q` / `Esc` | 中止する |

   粒子が左右両方に見えていることを確認してから押します。片眼にしか見えていない場合は
   `Cannot accept until both camera streams have produced frames` と出て受け付けません。
3. プレビューが閉じ、カメラを開き直してハード同期（左Master／右Slave）を確立します。
4. **ターミナル**に次が出ます。ここでの `Enter` が録画開始です。

   ```text
   [REC] Both cameras are streaming. Enter starts the saved interval (Q aborts):
   ```

   押した時点のマスターカメラ時計から10スライス先（`--delta-t-us 1000` なら10 ms）に
   アラインされた時刻が共有され、左右がその同じ区間を保存します。`Q` で中止できます。
5. 停止は `record_duration_sec` 次第です。

   | `record_duration_sec` | 挙動 |
   |---|---|
   | 2.0（既定） | `Enter` で開始 → 2.0 秒後に自動停止 |
   | 0 | `Enter` で開始 → ターミナルの2回目の `Enter` で停止 |

6. 撮影後、同期モード・保存区間の完走・イベント数上限未到達が検査され、runへ校正のコピーと
   ハッシュ、設定が保存されます。

**`Enter` は2回、押す場所が違います。** 1回目はプレビューウィンドウ（受理）、2回目はターミナル
（録画開始）です。プレビューと記録は別のカメラセッションなので、1つにまとめるには記録側の
マルチプロセス構造を書き換える必要があり、そこは触っていません。プレビューの確認が要らない場合は
`stereo_eventcam_record_sync.py` を直接 `--no-preview --start-trigger enter` で呼べば
ターミナルの `Enter` だけになりますが、両眼に粒子が写っているかの確認は失われます。

出力先:

```text
data\session_cal20260805\recordings\trial01\
  stereo_recording_manifest.json
  recording_config.json
  calibration\stereo_calibration.npz
  left\left_events.npz
  right\right_events.npz
```

## 5. 撮影結果の検査

`stereo_recording_manifest.json` を開いて次を確認します。撮影コマンドが正常終了していれば
すでに検査済みですが、目視でも確認しておくと後の切り分けが楽になります。

| 項目 | 期待値 |
|---|---|
| `hw_sync.mode` | `left-master` |
| `hw_sync.verified` | `true` |
| `left.meta.sync_role` | `master` |
| `right.meta.sync_role` | `slave` |
| `left.meta.capture_interval_complete` | `true`（右も同様） |
| `left.meta.event_limit_reached` | `false`（右も同様） |

`left_events.npz` と `right_events.npz` のサイズが極端に違う場合は、片眼の視野・フォーカス・
照明を疑ってください。粒子が片方にしか写っていない可能性があります。

## 6. 粒子抽出と3D復元

```powershell
& $PY .\eventcam_workflow.py process --run $RUN
```

内部では、左右それぞれの2D抽出 → 時刻対応付け → 三角測量 → 軌跡図、の順に実行されます。
カメラには接続しません。中断した場合は同じ設定のまま `--resume` を付けて再実行できます。

```powershell
& $PY .\eventcam_workflow.py process --run $RUN --resume
```

出力が追加されます:

```text
  left\event_tracking\
    event_centres_raw.csv
    event_centres_interp.csv / .npy
    event_tracking_summary.json
  right\event_tracking\          左と同じ構成
  stereo_3d\
    stereo_3d_points.npz / .csv
    stereo_3d_points_summary.json
    stereo_3d_trajectory.png
    processing_manifest.json
  standalone_processing.json     status=complete で完走
```

## 7. 抽出結果の検査

### 7-1. 片眼の2D抽出 — `left/right の event_tracking_summary.json`

| キー | 見方 |
|---|---|
| `raw_bins` | 生成された時間ビン数。`--duration-sec 2.0`／`hop_us 100` なら約20,000 |
| `valid_raw_points` | 粒子を取れたビン数。`raw_bins` に近いほど良い |
| `missing_raw_points` | 取れなかったビン数。多ければ `min_events`・`min_mass` が厳しすぎるか、粒子が暗い |
| `tracking_rejected_points` | `max_step_px 15` を超えて棄却された数。多ければ別の発光物へ飛び移っている疑い |
| `interp_points` | 補間後の点数。`dt_us 100` なら約20,000 |
| `event_count.avg` | 1ビンあたりの平均イベント数。`min_events 20` を大きく下回るなら露出・照明不足 |
| `component_event_mass.avg` | 採用した連結領域の質量。`min_mass 30` 付近しかないなら閾値が限界 |

左右どちらかだけ `valid_raw_points` が極端に少ない場合、その眼の視野・フォーカス・遮蔽を先に直します。
3Dの有効点は左右の積になるので、片眼が悪いと全体が悪くなります。

### 7-2. 3D復元 — `stereo_3d/stereo_3d_points_summary.json`

| キー | 見方 |
|---|---|
| `input_points` | 対応付けを試みた点数 |
| `valid_points` | 3D化できた点数。`input_points` に対する比が有効率 |
| `reprojection_error_px.left_rms` / `right_rms` | 小さいほど良い。`--max-reprojection-error-px 3.0` で棄却済み |
| `reprojection_error_px.left_max` / `right_max` | 3.0 に張り付いているなら棄却が効いている |
| `z_left_cam_mm.mean` | 実際のカメラ〜粒子距離と矛盾しないか。ここが合わなければ校正かカメラ配置を疑う |
| `coordinate_system` | `left OpenCV camera coordinates, units=mm; X right, Y down, Z forward` |

最後に `stereo_3d_trajectory.png` を開いて、軌跡が物理的にあり得る形かを見ます。
数値が良くても左右取り違えや校正ずれは図で気づけることがあります。

---

## 8. 共通入口を使わない場合（低レベル直接）

撮影と後処理を個別のスクリプトで行う場合です。**この経路で撮ったrunは
`eventcam_workflow.py process` では処理できません**（`recording_config.json` と
`calibration\` のコピーを作らないため）。撮影も後処理も低レベルで通してください。

```powershell
$RUNDIR = "$KIT\data\session_cal20260805\recordings\trial01_lowlevel"
$CAL    = "$KIT\data\session_cal20260805\calibration\stereo_calibration.npz"
```

### 8-1. 撮影

```powershell
& $PY .\stereo_eventcam_record_sync.py `
  --left-serial 00000508 --right-serial 00000509 `
  --hw-sync left-master `
  --run-dir $RUNDIR `
  --duration-sec 2.0 --delta-t-us 1000 --start-delay-sec 2.0 `
  --npz-compression none --max-events 0 `
  --sensor-width 1280 --sensor-height 720
```

撮影に校正NPZは不要です。`--stereo-calibration` を渡すこともできますが、manifestにパスを
記録するだけで撮影動作には使いません。

上の例は `--start-trigger auto`（既定）なので、受理後 `--start-delay-sec` 経過で自動的に始まります。
共通入口と同じくEnterで開始したい場合は `--start-trigger enter` を付けます。さらに `--duration-sec 0`
にすると2回目のEnterで停止します（`--duration-sec 0` は `--start-trigger enter` とハード同期が必須です）。

**serialは必ず指定してください。** 省略すると列挙順で `devices[0]` が左、`devices[1]` が右に
自動割当てされます。エラーにならないまま左右が入れ替わり、3Dが反転し得ます。

### 8-2. 抽出と3D復元を一括

```powershell
& $PY .\stereo_process_recording.py $RUNDIR --stereo-calibration $CAL `
  --window-us 200 --hop-us 100 --dt-us 100 `
  --max-interp-gap-sec 0.005 --max-step-px 15 `
  --roi 0,0,1280,720 --tracking-method event_weighted `
  --threshold-count 1 --min-events 20 --min-area 5 --min-mass 30 `
  --polarity all --max-time-gap-sec 0.001 --max-reprojection-error-px 3.0
```

### 8-3. さらに分解する場合

```powershell
& $PY .\eventcam_npz_track.py "$RUNDIR\left\left_events.npz" `
  --output-dir "$RUNDIR\left\event_tracking" `
  --window-us 200 --hop-us 100 --dt-us 100 `
  --max-interp-gap-sec 0.005 --max-step-px 15 `
  --roi 0,0,1280,720 --tracking-method event_weighted `
  --threshold-count 1 --min-events 20 --min-area 5 --min-mass 30 --polarity all

& $PY .\eventcam_npz_track.py "$RUNDIR\right\right_events.npz" `
  --output-dir "$RUNDIR\right\event_tracking" `
  --window-us 200 --hop-us 100 --dt-us 100 `
  --max-interp-gap-sec 0.005 --max-step-px 15 `
  --roi 0,0,1280,720 --tracking-method event_weighted `
  --threshold-count 1 --min-events 20 --min-area 5 --min-mass 30 --polarity all

& $PY .\stereo_triangulate_tracks.py `
  --left-track  "$RUNDIR\left\event_tracking\event_centres_interp.csv" `
  --right-track "$RUNDIR\right\event_tracking\event_centres_interp.csv" `
  --stereo-calibration $CAL `
  --output-dir "$RUNDIR\stereo_3d" `
  --max-time-gap-sec 0.001 --max-reprojection-error-px 3.0

& $PY .\stereo_plot_3d_points.py "$RUNDIR\stereo_3d\stereo_3d_points.npz"
```

---

## 9. うまくいかないとき

| 症状 | 最初に見るところ |
|---|---|
| `[FAIL] OpenEB` | `EVENTCAM_OPENEB_ROOT` の綴り。`activate_metavision_env.ps1` の警告行 |
| `Two cameras are required.` | `devices` で2台見えるか。USB・電源・ケーブル |
| `hw_sync.verified` が false | SYNC配線の向き（左OUT→右IN）。`--hw-sync-timeout-sec` を延ばす |
| `Refusing to overwrite/reuse existing output` | `--run` 名が既存。新しい名前にする |
| `valid_raw_points` が極端に少ない | 照明・フォーカス・露出。`min_events` `min_mass` を下げて再解析 |
| `tracking_rejected_points` が多い | 別の発光物へ飛び移っている。`roi` を絞るか `left_mask_roi` で除外 |
| 3Dの `valid_points` が少ない | まず片眼の2D有効率を見る。両眼とも良ければ `max_time_gap_sec` と `max_reprojection_error_px` |
| `z_left_cam_mm.mean` が実距離と合わない | 校正がこのカメラ配置に対応しているか。動かしていれば再校正 |

解析条件を変えて試す場合は、元のrunを保持したまま別ディレクトリへコピーし、そのコピーに対して
実行してください。同じrunに異なる設定で `--resume` することはできません。

## 10. 次の計測

`$RUN` を新しい名前に変えて §4 から繰り返します。校正は同じものが使われます。
カメラの固定・レンズ・フォーカス・相対姿勢を動かした場合は、校正が無効になるので
[README_JP.md](README_JP.md) §3 の手順で新しい `session_dir` に校正し直してください。

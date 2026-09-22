# 単眼サンプル（撮影・動画化・粒子抽出）

作成: 2026-09-16。カメラ1台だけで、**撮影 → 粒子抽出 → 重畳動画**まで通すサンプルです。
ステレオ、ハードウェア同期、ステレオ校正、3D復元は使いません。

ステレオ計測の手順は [RECORDING_RUNBOOK_JP.md](RECORDING_RUNBOOK_JP.md) を参照してください。

**撮影開始はどちらも `Enter`** ですが、単眼はプレビューウィンドウ、ステレオはターミナルで押します。詳しくは §2 の「ステレオ側との違い」を参照してください。

## ステレオ版との違い

| | ステレオ（`eventcam_workflow.py`） | 単眼（`mono_sample.py`） |
|---|---|---|
| カメラ | 2台、左Master／右Slave | 1台 |
| ハード同期 | 必須（`left-master`） | 使わない |
| ステレオ校正 | 必須 | **不要** |
| 出力座標 | 左カメラ座標、mm | **画像座標、px** |
| 奥行き | あり | なし |
| 保存先 | `data/<session>/recordings/<run>/` | `mono_records/<run>/` |

**出力はピクセルです。** 校正していないので mm にはなりません。平面内の運動を mm で
評価したい場合は、同梱の `eventcam_scale_calibration_capture.py` を既知直径の円で実行して
px/mm を求め、その倍率を後から掛けてください。奥行きが変わる運動では、その倍率は成立しません。

## 0. 前提

```powershell
$KIT = "C:\Users\digit\Documents\scripts\python\eventcam_control\eventcam_standalone"
$PY  = "C:\Users\digit\Documents\scripts\python\eventcam_control\venv\Scripts\python.exe"

cd $KIT
$env:EVENTCAM_OPENEB_ROOT = "C:\Users\digit\Documents\scripts\python\eventcam_control\openeb_install"
$env:EVENTCAM_VCPKG_BIN   = "C:\Users\digit\Documents\scripts\python\eventcam_control\vcpkg\vcpkg_installed\x64-windows\bin"
```

## 1. 一括で通す

```powershell
& $PY .\mono_sample.py all --run demo01 --serial 00000508 --dry-run
& $PY .\mono_sample.py all --run demo01 --serial 00000508
```

`record` → `track` → `video` を順に実行します。`--run` は毎回変えます。既存の出力がある場合は、
カメラを開く前に停止します。

## 2. 工程ごとに実行する

```powershell
& $PY .\mono_sample.py devices
& $PY .\mono_sample.py record --run demo01 --serial 00000508
& $PY .\mono_sample.py track  --run demo01
& $PY .\mono_sample.py video  --run demo01
```

| アクション | 処理 | カメラ |
|---|---|---|
| `devices` | デバイス一覧 | 列挙のみ |
| `record` | 1台で撮影し、RAWとNPZを保存 | 開く |
| `track` | NPZから粒子中心を抽出 | 開かない |
| `video` | イベント動画に抽出結果を重畳 | 開かない |
| `all` | 上の3つを順に | 開く |

すべてに `--dry-run` を付けられます。呼び出す予定のコマンドだけを表示し、カメラSDKもimportしません。

### 撮影中の操作

`record` を実行するとプレビューウィンドウが開きます。**録画は必ず `Enter` で始まります。**
`--duration-sec` が決めるのは止め方だけです。

| 操作 | 動作 |
|---|---|
| `Enter`（1回目） | 録画開始。画面左上に赤い `REC` が出る |
| `Enter`（2回目） | 録画停止。`--duration-sec 0` のときだけ使う |
| `Esc` / `Q` | 録画中なら停止して終了、そうでなければそのまま終了 |

| `--duration-sec` | 挙動 |
|---|---|
| 2.0（既定） | `Enter` で開始 → 2.0 秒後に自動停止 |
| 0 | `Enter` で開始 → もう一度 `Enter` で停止 |

`Enter` は**ターミナルではなくプレビューウィンドウ**で押します。ウィンドウにフォーカスを当ててください。
粒子が視野内にあり、十分イベントが出ていることを確認してから開始します。

### ステレオ側との違い

同じ `Enter` でも押す場所と回数が違います。

| | 単眼 `mono_sample.py record` | ステレオ `eventcam_workflow.py record` |
|---|---|---|
| `Enter` の回数 | 1回（開始）、手動停止なら2回 | 2回（受理→開始）、手動停止なら3回 |
| 開始の `Enter` を押す場所 | プレビューウィンドウ | **ターミナル** |
| 開始から記録まで | 即時 | マスターカメラ時計で10スライス先（既定10 ms） |
| 停止 | `--duration-sec`、または2回目の `Enter` | `record_duration_sec`、または `0` で手動停止 |
| 受理の条件 | なし | 左右両方がフレームを出していること |

ステレオは**プレビューと記録が別のカメラセッション**です。プレビューを受理するとカメラを
開き直してハード同期を確立し、その後ターミナルで `Enter` を待ちます。そのため開始の `Enter` は
ウィンドウではなくターミナルで押します。単眼はプレビューと記録が同じセッションなので、
ウィンドウの `Enter` がそのまま記録の開始点です。

## 3. 出力

```text
mono_records/demo01/
  mono_<日時>.raw                  元のRAW
  mono_<日時>.json                 撮影統計
  mono_<日時>_events.npz           抽出・動画化の入力
  mono_recording_manifest.json     serial・設定・イベント数
  event_tracking/
    event_centres_raw.csv          ビンごとの中心（px）
    event_centres_interp.csv/.npy  等間隔補間後の軌跡（px）
    event_tracking_summary.json    抽出の統計
    processed_centres.npy
    t_xy.png / xy.png              時系列と軌跡の図
  mono_overlay.mp4                 重畳動画
  mono_overlay_timestamps.csv      各フレームの時刻
```

`mono_records/` は `.gitignore` の対象です。計測データはgitに乗りません。

## 4. 確認すること

撮影後、`mono_recording_manifest.json` の `stats.total_events` が0でないこと、
`sensor_size` が想定どおりであることを見ます。

抽出後、`event_tracking/event_tracking_summary.json` を見ます。

| キー | 見方 |
|---|---|
| `raw_bins` | 時間ビン数。2.0 s ／ hop 100 µs なら約 20,000 |
| `valid_raw_points` | 粒子を取れたビン数。`raw_bins` に近いほど良い |
| `missing_raw_points` | 多ければ `--min-events`・`--min-mass` が厳しすぎるか、粒子が暗い |
| `tracking_rejected_points` | `--max-step-px` 超で棄却。多ければ別の発光物へ飛び移っている |
| `event_count.avg` | `--min-events` を大きく下回るなら露出・照明不足 |

最後に `t_xy.png` と `mono_overlay.mp4` を見て、抽出した中心が実際の粒子に乗っているかを確認します。
数値だけでは、別の発光物を掴んでいても気づけません。

## 5. 主なオプション

既定値はステレオ側の運用値に揃えてあります。

| 分類 | オプション | 既定 |
|---|---|---|
| 撮影 | `--duration-sec` | 2.0（Enterで開始し2.0秒で自動停止。0なら2回目のEnterで停止） |
| 撮影 | `--delta-t-us` | 10000 |
| 撮影 | `--npz-delta-t-us` | 1000 |
| 抽出 | `--window-us` / `--hop-us` / `--dt-us` | 200 / 100 / 100 |
| 抽出 | `--max-interp-gap-sec` / `--max-step-px` | 0.005 / 15 |
| 抽出 | `--tracking-method` | `event_weighted` |
| 抽出 | `--threshold-count` / `--min-events` / `--min-area` / `--min-mass` | 1 / 20 / 5 / 30 |
| 抽出 | `--polarity` | `all` |
| 抽出 | `--roi` / `--mask-roi` | 空（全面／除外なし） |
| 動画 | `--video-fps` | 2000 |
| 動画 | `--video-playback-fps` | 30 |
| 動画 | `--video-accumulation-us` | 1000 |
| 動画 | `--video-duration-sec` | 1.0（0で全区間） |

`--video-fps 2000` は「イベント時間の1秒を2000フレームに描く」という意味で、
`--video-playback-fps 30` が再生速度です。既定では 1/66.7 倍のスロー再生になります。
長い記録で `--video-duration-sec 0` を指定すると、フレーム数とファイルサイズが大きくなります。

## 6. 低レベルスクリプトを直接使う

`mono_sample.py` は次の3つを呼んでいるだけです。個別に条件を変えたい場合は直接実行できます。

```powershell
$RUN = "$KIT\mono_records\demo02"

& $PY .\mono_eventcam_record.py --serial 00000508 --run-dir $RUN --duration-sec 2.0

& $PY .\eventcam_npz_track.py "$RUN\mono_20260916_120000_events.npz" `
  --output-dir "$RUN\event_tracking" `
  --window-us 200 --hop-us 100 --dt-us 100 `
  --max-interp-gap-sec 0.005 --max-step-px 15 `
  --tracking-method event_weighted `
  --threshold-count 1 --min-events 20 --min-area 5 --min-mass 30 --polarity all

& $PY .\eventcam_npz_render_video.py "$RUN\mono_20260916_120000_events.npz" `
  --output "$RUN\mono_overlay.mp4" `
  --fps 2000 --video-fps 30 --accumulation-us 1000 --duration-sec 1.0 --draw-time `
  --tracking-csv "$RUN\event_tracking\event_centres_interp.csv"
```

NPZのファイル名は撮影時刻で決まるので、実際の名前に置き換えてください。
`mono_eventcam_record.py` はカメラ操作を `eventcam_scale_calibration_capture.py` の
実績ある関数（プレビュー、RAW記録、RAW→NPZ変換）に委ねていて、円フィットだけを外したものです。

## 7. 検証済みの範囲

`selftest.py` に単眼の回帰テストが2件入っています。実機は不要です。

- 既知の正弦軌道を持つ合成イベントから `track` と `video` を実行し、抽出した中心が
  真値と1 px以内で一致すること、欠測と棄却が0であること、mp4が生成されること、
  同じrunへの再実行が拒否されることを確認します。
- `mono_sample.py all --dry-run` の出力に stereo・calibration・hw-sync・左右serialが
  一切現れないこと、呼び出し先が3スクリプトだけであることを確認します。

実機での単眼撮影そのものは、このパッケージ作成時には未実施です。
撮影経路は既存の `eventcam_scale_calibration_capture.py` と同じコードですが、
最初は短い記録で確認してください。

```powershell
& $PY -B .\selftest.py -v
```

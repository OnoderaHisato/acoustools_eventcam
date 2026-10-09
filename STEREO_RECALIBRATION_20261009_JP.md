# ステレオ再校正の手順（2026-10-09、冷却機構の取り付けとカメラの治具のあと）

## なぜ必要か

冷却機構の取り付けでカメラの下に治具を入れ、緩んだレンズも付け直しました。

- 10/08 のテスト記録 2 本では、左右の対応のずれ（右画像でのエピポーラ距離の中央値）が、9/25 の +0.69 px から −2.6〜−2.8 px に変わりました。
- 3D の再投影誤差は、約 0.4 px から約 1.4 px に増えています。
- 詳細は `PROJECT_HANDOFF.md` の 2026-10-09 の項にあります。

## 方針：単眼の校正（左右それぞれ）からやり直す

- ピントリングと絞りは触っていません。ただし、レンズを付け外ししたので、レンズの座りがわずかに変わっています。画像の中心・焦点距離・歪みが変わっている可能性があるため、単眼の内部校正からやり直します。
- 新しい内部パラメータは、8/5 の値と比べます。8/5 の値は `intrinsics_from_calib_20260805/` に書き出してあります。このファイルで 8/5 の画像から計算し直すと、8/5 の校正と完全に一致しました。
- 比べて差が誤差の範囲（焦点距離で 0.1〜0.2 % 程度、中心で 1〜2 px 程度）なら、どちらで計算しても結果はほぼ同じです。その場合も、新しく測った値を使います。

## 0. 撮る前に

1. **レンズをしっかり締める。** 緩みが再発すると、校正がまたずれます。
2. カメラ・治具を最終の状態に固定する。ピントが甘くなっていないか、プレビューで粒子や盤面の像が鮮明かを確かめる。ピントを直すなら校正の前に直す。**校正のあとは、冷却後の再計測が終わるまで、カメラとレンズに触れない。**
3. PAT は止めておく（音は出さない）。粒子は要らない。
4. 同期ケーブル（左 Master の SYNC_OUT → 右 Slave の SYNC_IN）がつながっていることを確かめる。
5. `checkerboard_10x7_normal.png` をモニタに表示し、画面上のマス 1 辺が **7.12 mm** であることを定規で測る。表示倍率は、全部の撮影が終わるまで変えない（点滅する gif は使わない）。

## 1. 左カメラの単眼の画像を撮る（カメラを開く）

```powershell
.\venv\Scripts\python.exe eventcam_checkerboard_calibration_capture.py --serial 00000508 --output-dir checkerboard_calib_left_20261009 --checkerboard-image checkerboard_10x7_normal.png --square-mm 7.12 --require-square-mm 7.12 --duration-sec 0.6 --frame-window-us 20000 --frame-window-us-list 10000,20000,30000,40000 --render-mode all --candidate-count 4
```

- 30〜50 姿勢を撮ります。画面の中央だけでなく四隅まで、近い・遠い、傾き（左右・上下・回転）を変えて、盤面全体が画像に入るように撮ります。
- プレビュー窓のキー: Enter で 1 姿勢を撮る、**R でカメラを開き直す**（たまったイベントを捨てて、今の時刻から表示し直す。プレビューが遅れたり止まったりしたとき用。撮影中は効かない）、Esc／Q で終える。
- 単眼では、盤面は左右で同じ姿勢である必要はありません。そのカメラの画面全体を覆うことが大事です。

## 2. 右カメラの単眼の画像を撮る（カメラを開く）

```powershell
.\venv\Scripts\python.exe eventcam_checkerboard_calibration_capture.py --serial 00000509 --output-dir checkerboard_calib_right_20261009 --checkerboard-image checkerboard_10x7_normal.png --square-mm 7.12 --require-square-mm 7.12 --duration-sec 0.6 --frame-window-us 20000 --frame-window-us-list 10000,20000,30000,40000 --render-mode all --candidate-count 4
```

## 3. 単眼の校正を計算する（カメラは開かない）

```powershell
.\venv\Scripts\python.exe single_camera_calibrate.py --images checkerboard_calib_left_20261009\calib_images --camera-serial 00000508 --square-mm 7.12 --require-square-mm 7.12 --output checkerboard_calib_left_20261009\single_calibration_square7p12_left00508.npz --debug-dir checkerboard_calib_left_20261009\calibration_debug_square7p12
```

```powershell
.\venv\Scripts\python.exe single_camera_calibrate.py --images checkerboard_calib_right_20261009\calib_images --camera-serial 00000509 --square-mm 7.12 --require-square-mm 7.12 --output checkerboard_calib_right_20261009\single_calibration_square7p12_right00509.npz --debug-dir checkerboard_calib_right_20261009\calibration_debug_square7p12
```

Claude が、debug 画像（全姿勢で 9×6 のコーナーが正しいか）、姿勢ごとの誤差、8/5 の値との差を確かめます。旧実験の単眼の RMS は約 0.18〜0.19 px でした。

## 4. 左右同時の校正画像を撮る（カメラを開く。約 30〜60 分）

```powershell
.\venv\Scripts\python.exe stereo_checkerboard_sync_capture.py --left-serial 00000508 --right-serial 00000509 --output-dir stereo_checkerboard_calib_extrinsics_20261009 --checkerboard-image checkerboard_10x7_normal.png --square-mm 7.12 --pose-count 30 --hw-sync left-master --duration-sec 0.05 --frame-window-us-list 5000,10000,20000 --preferred-frame-window-us 10000
```

- 撮影の設定は 8/5 と同じです。8/5 は 51 回試して 31 姿勢が採用され、計算には 23 ペアを使いました。
- 盤面は粒子がいる場所のあたり（左カメラから約 170 mm）に置き、両方のカメラに盤面全体が写るようにします。1 姿勢ごとにプレビューが出るので、置いたら Enter を押します。
- 左右の両方で同じ時刻に 9×6 のコーナーが見つかったときだけ採用されます。姿勢の偏りに注意します（8/5 は似た姿勢の 8 本を外しました）。

## 5. ステレオ校正を計算する（カメラは開かない）

試しの計算（3 章の新しい内部パラメータを使う）:

```powershell
.\venv\Scripts\python.exe stereo_camera_calibrate.py --left-images stereo_checkerboard_calib_extrinsics_20261009\left\calib_images --right-images stereo_checkerboard_calib_extrinsics_20261009\right\calib_images --left-intrinsics checkerboard_calib_left_20261009\single_calibration_square7p12_left00508.npz --right-intrinsics checkerboard_calib_right_20261009\single_calibration_square7p12_right00509.npz --left-serial 00000508 --right-serial 00000509 --square-mm 7.12 --require-square-mm 7.12 --min-pairs 20 --output stereo_checkerboard_calib_extrinsics_20261009\stereo_calibration_square7p12_trial.npz --debug-dir stereo_checkerboard_calib_extrinsics_20261009\stereo_debug_trial
```

Claude が CSV の姿勢ごとの誤差を見て、外す姿勢を提案します。最終の計算:

```powershell
.\venv\Scripts\python.exe stereo_camera_calibrate.py --left-images stereo_checkerboard_calib_extrinsics_20261009\left\calib_images --right-images stereo_checkerboard_calib_extrinsics_20261009\right\calib_images --left-intrinsics checkerboard_calib_left_20261009\single_calibration_square7p12_left00508.npz --right-intrinsics checkerboard_calib_right_20261009\single_calibration_square7p12_right00509.npz --left-serial 00000508 --right-serial 00000509 --square-mm 7.12 --require-square-mm 7.12 --min-pairs 20 --exclude-pairs [外す姿勢] --output stereo_checkerboard_calib_extrinsics_20261009\stereo_calibration_square7p12_final.npz --debug-dir stereo_checkerboard_calib_extrinsics_20261009\stereo_debug_final
```

外す姿勢がなければ `--exclude-pairs` を省きます。

合格の目安（8/5 の値を参考に）:

- 使ったペアが 20 以上ある。
- stereo RMS が 0.36 px 前後である。
- 基線長が約 120.5 mm である。
- 姿勢ごとの左右の PnP RMS（8/5 は約 0.22〜0.24 px）とエピポーラ RMS に、極端な外れがない。

比較のため、Claude は 8/5 の内部パラメータ（`intrinsics_from_calib_20260805/`）でも同じ計算をし、違いを報告します。

## 6. 確かめる（Claude が行う。カメラは開かない）

- 10/08 のテスト記録 2 本に新しい校正を当て、左右の対応のずれが 0 付近に戻るか（9/25 は +0.69 px）、再投影誤差が 0.4 px 程度に戻るかを見る。
- ただし 10/08 の記録は、レンズを締め直す前の状態かもしれない。校正のあとに同じテスト軌道を 1 本撮っておくと、校正と同じカメラの状態で確かめられる。

## 7. 新しい校正を使うようにする（ユーザーの判断）

- 校正ファイルを無断で切り替えない決まりなので、上の確認のあとに切り替えるかを決めてもらいます。切り替えるときは、`stereo_acoustools_3d_common.py` の `DEFAULT_CALIBRATION` を新しい final の NPZ にします。
- 記録そのもの（イベント）は校正に依存しません。run の manifest の `stereo_calibration` に、処理のときに使う校正のパスが書かれます。切り替える前に撮った run も、処理のときに manifest のパスを新しい校正にすれば正しく 3D 化できます。ただし、カメラの状態が校正と同じであることが前提です。
- サーバー（dngstation／deepstation／Miyabi）にも新しい校正ファイルを置きます。

## 8. 処理のときの左カメラのマスク

LED の写る位置は、9/25 は x 770〜820・y 0〜20、10/08 は x 約 735〜785・y 約 50〜100、10/09 のレンズの付け直しのあとは x 808〜885・y 48〜148 でした。10/09 の位置は、60 mm のカーディオイドと半径 28 mm の XZ 走査が左画像で通る場所に重なっていました。

そのため 10/09 に LED を動かし、今は左画像の右上の角（x 1182〜1278、y 0〜99）に写ります。計画のどの軌道からも 310 px 以上離れています。

**冷却後の記録は、左の追跡マスクを `1160,0,1280,120` にして処理します。**

- 手元で処理するとき: `--left-mask-roi-override 1160,0,1280,120`
- サーバーで処理するとき: `LEFT_MASK=1160,0,1280,120 bash run_leftmask_reprocess_docker.sh <list>`

後処理の既定（LED の ROI `600,0,1280,180` 全体を隠す）や、9/26 の `748,0,892,52` では処理しません。LED の検出の ROI はそのままで、検出は良好です（ピークは閾値の 32 倍）。

## 9. カメラ → PAT の変換について

- 解析の比較（`ideal_comparison_3d`、`reference_comparison_3d`）と場の地図は、固定の変換（`camera_to_pat_pooled_ffheart_20260918`）で PAT の座標にしています。
- 今の配置にこの変換を当てると、約 1.9 mm ずれ、回転も 2〜4° 変わります。差やループの平均を取る量なら、ずれの大部分は打ち消されます。一方、回転は x/y/z の成分を混ぜ、位置のずれは場の地図 w(u) の位置を動かします。
- 作り直しの方法は 2 通りあります。
  - (a) 冷却後の記録（kcheck・走査など）から、解析側が以前と同じ方法で作り直す。新しい計測は要らない。
  - (b) PAT の格子点 27 点で登録する（`pat_stereo_grid_capture.py`）。ただし、設定 `pat_stereo_grid_config.json` が 7/28 の校正と Ossila ステージを前提にした古いままなので、手直しが要る。
- どちらにするかは、解析側と相談して決める。

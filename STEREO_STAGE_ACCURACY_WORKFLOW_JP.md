# リニアステージによるステレオ奥行・局所3D精度評価

## 結論

奥行精度の主評価には「中心1点のステージ往復走査」を使い、8頂点の直方体は
「距離ごとの局所スケール、歪み、軸間干渉」を調べる副評価にするのが適切です。
8頂点の辺長が正しくても、直方体全体が奥行方向へ同じ量だけずれる可能性があるため、
8頂点だけでは絶対奥行誤差を検出できません。

実装した実験は、各ステージ位置で次を行います。

1. 基準位置で専用registration pass（中心前、8頂点、中心後）を1回取得
2. 各距離で中心点を静止計測
3. \((\pm x,\pm y,\pm z)\) の全組合せ、すなわち直方体の8頂点を静止計測
4. 中心点をもう一度静止計測
5. 全距離を近→遠、遠→近の両方向で5 cycle繰り返す

頂点間は一度に1軸の符号だけを変える Gray-code 順です。連続運動中の位置を主評価に
使わず、粒子が停止・settleした後の短い記録を1点ずつ処理します。この方法なら、
PATの運動遅れや時間同期誤差を静的なステレオ精度へ混ぜにくくなります。

## 座標系

三角測量の一次出力は左OpenCVカメラ座標です。

- X: 画像の右
- Y: 画像の下
- Z: カメラ前方
- 単位: mm

PATのY軸を、そのままカメラZ軸とみなしてはいけません。カメラが斜めに設置されて
いるため、PAT Y方向は通常、左カメラX/Y/Zの複数成分へ現れます。今回の標準設定では、
別に実施した27点PAT登録の `camera_to_pat_transform.json` を固定して使い、最終的な
位置・残差をPAT座標で出力します。ステージ実験内の専用8頂点は座標fitに再利用せず、
登録状態が維持されているかを確認するverificationデータです。

設定の

```json
"moving_body": "camera",
"moving_body_translation_per_global_mm_in_pat": [0.0, 1.0, 0.0]
```

は「Ossila global座標が +1 mm増えると、カメラがPAT +Yへ1 mm動く」という意味です。
hardware 200 mmで登録した変換を全地点へそのまま適用してはいけません。登録時global
readbackを \(g_0\)、各captureのreadbackを \(g_i\)、上記の物理移動ベクトルを \(q\)
とすると、各地点の変換は

\[
p_{\mathrm{PAT}}=R_0p_{\mathrm{camera}}+t_0+q(g_i-g_0)
\]

です。実装はcommand値ではなく、transformに保存した登録時hardware readbackと各capture
の実readbackからこの並進を合成します。PAT指令値を真値として
`residual_pat_mm = measured_pat_mm - pat_target_mm` を計算します。

生の左カメラ座標、camera Z、斜距離、ベースラインへの垂線距離も診断値として残します。
したがってカメラ座標を計算から完全に無くすのではなく、精度判定の主座標をPATへ移します。
この合成はステージが純並進しカメラ姿勢が一定というモデルです。ステージのpitch/yaw、
取付角、ケーブル反力による姿勢変化は実験系の誤差として残ります。

ステージだけで直接分かる真値は相対変位です。カメラ光学中心からの絶対距離まで
評価するには、基準位置の絶対距離をレーザー測長器などで別途与える必要があります。
それがない場合、本実験の厳密な名称は「奥行方向の相対変位追従精度・線形性」です。

## 理論値

平行ステレオの近似は

\[
Z=\frac{fB}{d}
\]

です。ここで、\(f\) はpixel単位の焦点距離、\(B\) は光学中心間の基線、
\(d\) は視差です。視差ノイズに対する奥行ノイズは

\[
\sigma_Z \simeq \frac{Z^2}{fB}\sigma_d
\]

となるため、理想的なランダム誤差でも距離の2乗で悪化します。左右の画像位置誤差が
独立で同じ標準偏差 \(\sigma_{px}\) なら、

\[
\sigma_d \simeq \sqrt{2}\sigma_{px}
\]

です。

以下は旧校正に基づく設計例です。今回作成する `square_size_mm=7.12` の校正NPZから、
解析時に実値を再計算します。旧 `7.1 mm` 校正を実行・流用してはいけません。

- 生intrinsicの平均 \(f_x\): 約 `1771.458 px`
- 光学中心基線: 約 `120.385 mm`
- \(fB\): 約 `213256.7 px mm`
- stereo calibration RMS: `0.323912 px`
- 左右カメラ相対回転: 約39度

単純式での奥行感度は次の通りです。

| 左カメラZ | \(dZ/dd\) | \(\sigma_d=0.5 px\) の1σ |
|---:|---:|---:|
| 300 mm | 0.422 mm/px | 0.211 mm |
| 500 mm | 1.172 mm/px | 0.586 mm |
| 750 mm | 2.637 mm/px | 1.319 mm |
| 1000 mm | 4.689 mm/px | 2.344 mm |

「95%相当のランダム誤差が1 mm以内」と置いた簡易限界は、左右各カメラの位置ノイズを
それぞれ次のように仮定した場合です。

| 各カメラの位置σ | 簡易的な距離限界 |
|---:|---:|
| 0.10 px | 約877 mm |
| 0.25 px | 約555 mm |
| 0.323912 px | 約487 mm |
| 0.50 px | 約392 mm |

`0.323912 px` は校正時のstereo RMSであり、粒子追跡ノイズそのものではありません。
上表は設計目安です。また現在のカメラは約39度の収束配置なので、実装した解析では
単純視差式だけでなく、実際の `undistortPoints`＋`triangulatePoints` を各実測点で
数値微分し、

\[
\Sigma_p=J\Sigma_mJ^T
\]

によりステージ軸方向の理論σを出します。それでも、理論値には次が含まれません。

- カメラ校正の距離依存バイアス、歪みモデル残差
- チェッカーボード実寸誤差による全体スケール誤差
- PAT指令位置と実際の粒子平衡位置の差
- リニアステージの取付角、ピッチ・ヨー、バックラッシュ
- 振動、熱ドリフト、追跡対象の取り違え

したがって、実測確認は必要です。

数値ヤコビアンの理論σは「1 tracking windowを1回三角測量した点」に対する値です。
一方、実測中心は同じcapture内の多数windowのrobust medianです。window間には相関が
あるため、単純に \(\sqrt{N}\) で割ったり、両者を同じ推定量のσとして直接比較したり
しません。

## 実装ファイル

- `stereo_stage_accuracy_capture.py`: 安全確認、計画、ステージ/PAT移動、ステレオ記録
- `stereo_stage_accuracy_analyze.py`: 追跡・三角測量、静止点集約、距離別評価
- `stereo_stage_accuracy_common.py`: 計画、剛体fit、理論ヤコビアン、統計
- `stereo_stage_accuracy_config.json`: 実験条件
- `test_stereo_stage_accuracy.py`: 幾何と合成セッションのテスト

既存の次の資産を再利用します。

- `Ossila_LinearStage_Python_sample/ossila_stage/stage.py`
- `stereo_eventcam_record_sync.py`
- `stereo_process_recording.py`
- `stereo_triangulate_tracks.py`
- `pat_stereo_grid_capture.py` のPAT静止移動処理

## Ossilaサンプルに対する安全対策

元の非公式サンプルにはソフトリミットがなく、反転軸の相対移動とglobal現在位置にも
問題があります。今回のコードは次の方針です。

- `--execute` がない限り一切ハードウェアを開かない
- 設定した1軸だけを開く
- 相対移動を使わず、raw hardware絶対座標へ変換して移動
- 接続前に全計画点をソフトリミット検査
- 接続直後に最初のcommandとしてstatusを読み、既動作中なら即stopして実行拒否
- stage/PATを動かす前に、左右cameraを実際に短時間open/streamし、
  指定したMaster/Slave同期と共通camera timestamp区間の完了を検証
- 現在位置→基準位置の経路を表示し、soft limit外なら専用home確認なしでは拒否
- `<device?>`, `<serial?>`, `<length?>`, `<alarms?>`, `<posmode?>`, `<status?>`
  を厳密なフレームとして確認し、不一致を握りつぶさず移動拒否
- `<device?>` の製品コードと内部serialを別々に実行ロック
- firmwareが200 mm stageに対して `<length 200000>` を返す場合は、
  設定travelとの1000倍関係が厳密に成立するときだけµm生値として200 mmへ正規化
- `goto` には毎回 `5.0 mm/s` を明示（速度省略時の最大速度を使わない）
- 実機のacc/decがconfigの承認値と一致しなければ移動拒否
- statusの停止を2回確認してからsettle
- raw `pos?` を `global=(raw-datum)/direction` へ変換し、到着許容差を確認
- timeout、読値異常、例外、Ctrl+Cではmanifest書込みより先にstop/hardstopを試行
- エラー時は予期しない自動復帰移動をしない
- 通常完了時だけ、設定に従って基準位置へ戻る
- device、firmware、serial、length、speed、acc、dec、alarms、posmode、status、
  位置応答をmanifestへ保存
- 校正NPZ、Ossila設定、camera-to-PAT変換JSONをsessionへコピーし、hashが変わった
  sessionの再開を拒否
- PAT初期化途中の例外でも、送信済みhologramがあればOFF frameを試行

設定のsoft limitは現在 `[5,200] mm`、計画するhardware位置は
`200,180,...,20,5 mm` です。
これは安全を保証する値ではありません。治具、ケーブル、実ステージ長を確認して
実機に合わせて狭く設定してください。

`speed_mm_s` は現在 `5.0 mm/s` と明示しています。実機と治具に対して安全かを
小範囲のshakedownで確認し、必要ならさらに下げてください。Ossila公式マニュアルでは
`goto` の速度を省略すると最大速度になるため、この実装では省略を許しません。
[Ossila Linear Stage User Manual](https://downloads.ossila.com/manuals/linear-stage-user-manual.pdf)

## 初回の実行手順

### 1. 依存関係

Ossila接続には `pyserial` が必要です。

```powershell
.\venv\Scripts\python.exe -m pip install pyserial
```

### 2. 設定確認

特に次を実機と照合してください。

- `stage.axis`（物理/カメラ座標軸ではなく、`config.md`内の論理ラベル）
- Ossila `config.md` のシリアル番号と `direction`
- `expected_device_response`（例: `G2010B1`）
- `expected_stage_serial_response`（物理的にラベルした奥行stageの内部serial）
- `expected_acceleration_mm_s2`, `expected_deceleration_mm_s2`
  （治具・搭載物に対して承認したMotion Console設定）
- `datum_mm`
- `hardware_min_mm`, `hardware_max_mm`
- `positions_global_mm`
- `moving_body` と `moving_body_translation_per_global_mm_in_pat` の符号
- `analysis.camera_to_pat_transform`（hardware 200 mm付近で取得した品質PASSのJSON）
- PAT中心と直方体半辺
- 同期ケーブルのMaster/Slave向き

標準configでは `expected_device_response`, `expected_stage_serial_response` と
期待acc/decを意図的に空欄に
しています。これは実行ロックです。Ossila Motion Control Consoleまたはstageラベルで
対象stageの製品コード、内部serialと、治具・搭載物に対して安全なacc/decを確認して
設定するまで、`--execute` は接続・移動へ進みません。`config.md` の値はCOMポート選択用
USB serialであり、製品コード・内部serialとは別物です。acc/decは実機から読んだ値との
一致も確認します。

接続されているUSB serialとCOM portだけを、portを開かず表示するには:

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py --list-stage-ports
```

複数stageがある場合は、電源と機構を停止した状態でUSBケーブルを1本ずつ抜き差しし、
どの `E662...` が消えるかを記録してください。そのUSB serialに付いたX/Y/Zは単なる
論理名です。カメラ搭載stageが `E662608797387B2E` なら、現在の `stage.axis="y"` は
使えますが、OpenCVのY軸を意味しません。

Motion Consoleを使わず値を読む場合は、次のread-only probeを使えます。設定した1軸だけを
開き、最初にstatusを確認し、identity/settingsを読んでcloseします。home/goto、PAT、
カメラcommandは送りません。もし接続時点でstageが動いていればstopを試して失敗終了します。

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py `
  --execute `
  --probe-stage-identity
```

表示された `device`, `internal_serial`, `acceleration_mm_s2`,
`deceleration_mm_s2` をconfigへ転記し、実験用には新しいsessionでdry planから
やり直します。`length_raw_response_value=200000` かつ `length_mm=200` と出た場合は、
firmware生値を安全に正規化した記録です。

最初は、例えばステージ位置を `[-5,0,5] mm`、立方体を基準位置だけに減らして
小さなshakedownを行うのが安全です。

```json
"positions_global_mm": [-5.0, 0.0, 5.0]
```

```json
"cube_positions_global_mm": [0.0]
```

```json
"scan_cycles": 1
```

カメラ、同期ケーブル、USBケーブルを載せる場合は、最初のshakedownだけ
`"speed_mm_s": 1.0` 程度まで下げ、ケーブル張力と取付剛性を確認してください。

### 3. dry plan

次のコマンドはファイルへ計画を書くだけで、ステージ、PAT、カメラを開きません。
実験ごとに新しいsession名を決めてください。
その前に `analysis.camera_to_pat_transform` の `SELECT_SESSION` を、今回のPAT登録で
生成した `registration/camera_to_pat_transform.json` の実パスへ置換してください。
品質gate、ステレオ校正SHA-256、登録時stage readbackが一致しなければdry planを拒否します。

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py `
  --session-dir .\stereo_stage_accuracy_records\stage_trial_001
```

現在の標準設定では、verification用10点に加え、11距離×5 cycle×往復2方向×
（中心2点＋8頂点）なので、合計1110 captureです。
表示されるglobal位置、hardware位置、soft limit、軸、シリアルを確認してください。
`--execute` は新規sessionをその場で作れません。dry planの最後に表示される
`[NEXT]` command、または上で指定した同一の `--session-dir` を必ず再利用します。
これにより、確認したplan/config/calibration hashと実行対象が同じであることをロックします。

### 4. 実行

ステージがすでに原点復帰済みなら:

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py `
  --session-dir .\stereo_stage_accuracy_records\stage_trial_001 `
  --execute
```

この1軸を最初にhomeする必要がある場合のみ:

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py `
  --session-dir .\stereo_stage_accuracy_records\stage_trial_001 `
  --execute `
  --home-depth-axis
```

homeは実験用soft limitの外まで大きく動く可能性があります。`--home-depth-axis` と
`--yes` の併用は禁止しており、対象軸名を含む専用確認文の入力が必須です。

全12辺をゆっくり描いて目視確認するオプションは次です。これは定性的previewであり、
精度値には使いません。

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py `
  --session-dir .\stereo_stage_accuracy_records\stage_trial_001 `
  --execute `
  --trace-cube-preview
```

各点を直ちに処理して失敗を早く発見するには `--process-each` を追加します。ただし
実験時間は長くなります。

確認入力後も、最初のstage/PAT移動より前に、session内へ短いcamera health captureを
保存します。左右のopen、stream区間完了、設定したhardware syncが検証できない場合は
移動せず失敗します。全capture後も、基準位置への復帰、PAT OFF、stage状態確認とcloseが
成功するまでmanifestを `captured` にせず、`[DONE]` も表示しません。cleanup失敗sessionは
安全確認なしに再開できないsticky failureになります。

### 5. 中断後の再開

Ctrl+Cやcapture失敗後は、表示されたsessionを指定して再開します。処理済み点は
飛ばします。

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_capture.py `
  --config stereo_stage_accuracy_config.json `
  --session-dir stereo_stage_accuracy_records\SESSION_NAME `
  --execute
```

### 6. 解析

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_analyze.py `
  stereo_stage_accuracy_records\SESSION_NAME
```

すでに全点へ `stereo_3d_points.npz` がある場合:

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_analyze.py `
  stereo_stage_accuracy_records\SESSION_NAME `
  --skip-processing
```

理論表だけを生成する場合:

```powershell
.\venv\Scripts\python.exe stereo_stage_accuracy_analyze.py `
  --theory-only `
  --z-min-mm 200 `
  --z-max-mm 1200 `
  --z-step-mm 50
```

## 解析内容

### 1 captureごと

- 冒頭・末尾を除外
- 有効な3D点の中央値
- radial MADによる外れ除去
- robust sample数
- 生trackの有効sample率（標準で80%以上を要求）
- scatter RMS、P95、最大値
- ステージ指令位置とreadback

多数の500 µs窓は同じcapture内で相関するため、これらを独立反復とはみなしません。
往復scan/captureを独立反復として扱います。

### 各pass・各距離の中心

- center pre/postを平均して相殺せず、各captureのステージ軸方向誤差を個別評価
- passごとのbias、RMS、P95、最大絶対誤差、pre/post drift
- 軸直交方向誤差
- forward/reverse差
- camera Z
- 三角測量角
- 設定した画素σごとの数値ヤコビアン理論値

### 各pass・各距離の8頂点

- 往復・cycleごとに8頂点と12辺がすべて揃うことを確認
- 基準剛体変換に対する頂点RMSE/P95/最大誤差
- 12辺の方向別長さ誤差
- その距離だけでrigid fitした形状残差
- affine fitの3軸scale、体積scale、非直交性

局所rigid fit残差は「形の崩れ」を見ます。基準変換に対する頂点誤差は、
立方体全体の奥行ずれも含みます。両方を分けて保存します。

### 合格した測定点の範囲

標準設定では次をすべて満たす必要があります。

- 各passの中心capture最大絶対誤差 ≤ 1.0 mm
- 各passのcenter pre/postが2/2とも有効
- 各passで8/8頂点、12/12辺が有効
- 各passの8頂点RMSE ≤ 1.0 mm
- 各passの12辺最大絶対誤差 ≤ 1.0 mm
- 各passの静止点scatter P95中央値 ≤ 0.5 mm
- 全10 evaluation passが成功

基準距離から各方向へ、並んだ測定点が合格している範囲を報告します。これは列挙した
測定点での結果であり、20 mm間隔の点間を連続的に保証する表現ではありません。
途中で不合格になった後、離れた1点だけ再び合格しても範囲を延ばしません。

各captureの真値変位には、stage command差ではなく、専用registration passの
Ossila position readback中央値に対する各captureのreadback差を使います。command基準の
誤差も別列へ残すため、stage到達誤差を含む総合診断と分離できます。ただしstage自身の
readback校正誤差を独立に検証するには、レーザー変位計など外部測長器が必要です。

## 出力

sessionの `analysis/` に次を作ります。

- `stereo_stage_accuracy_report.md`: 日本語の主要結果
- `stereo_stage_accuracy_summary.json`: 行列を含む全結果
- `sample_measurements.csv`: capture単位
- `pass_accuracy.csv`: cycle・往復・距離ごとの合否
- `station_accuracy.csv`: 距離単位
- `cube_edges.csv`: 辺単位
- `stereo_stage_accuracy_summary.png`: 変位、奥行誤差、形状誤差
- `cube_shape_comparison.png`: 近・中・遠の直方体

## カメラ単独精度を求める場合

PAT粒子を真値にすると、評価結果は「PAT＋ステージ＋ステレオ」の総合精度です。
ステレオカメラ単独を評価したい場合は、ステージ上に小型の点滅LEDや既知寸法の
剛体3Dターゲットを載せる方法がより明快です。イベントカメラには点滅LEDが特に
適しています。

追加の診断点としては、8頂点よりも

```text
centre, +X, -X, +Y, -Y, +Z, -Z
```

の7点axis-starが、各軸の倍率、符号、クロストークを分離しやすいです。推奨する
最終構成は次です。

1. 全距離: 中心1点の多数往復
2. 近・中・遠: axis-star
3. 代表距離: 8頂点直方体
4. 必要なら最後に連続ワイヤーフレームを動的・定性的に確認

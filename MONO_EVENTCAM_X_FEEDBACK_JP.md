# 単眼イベントカメラ X軸フィードバック再現実験

## 目的

論文 *Precise position feedback control of acoustically levitating objects by
event-based vision* の最小構成を、現在のOpenMPD上下16x16セットアップで再現する。

- 左イベントカメラ `00000508` のみ使用
- PAT座標のX軸のみ補正
- 目標位置は中心 `0 mm` 固定
- 最初はPのみの小ゲインで符号、追跡、OpenMPDライブ送信時間を確認
- PID値は本セットアップの同定後に決める

既存のステレオ記録入口、左Master/右Slave設定、左LED ROI、ステレオ校正、既存計測成果物は変更しない。
この専用入口を実行するときの左カメラは明示的にMasterへ設定する。

## 追加されたファイル

- `mono_feedback_core.py`: ハードウェア非依存PID、座標射影、シミュレータ
- `mono_eventcam_1axis_feedback.py`: シミュレーション／実機入口
- `mono_feedback_compare.py`: baselineとfeedbackの軽量比較
- `test_mono_feedback_control.py`: 対象単体テスト

## 座標変換

既存のステレオ校正とcamera-to-PAT登録から、PAT原点近傍のX軸を左画像へ射影する。
現在の校正値では次のとおり。

- PAT Xの画像方向: およそ `(0.99965, -0.02657)`
- スケール: `7.8554 px/mm`
- 校正モデル上のPAT原点: `(525.44, 442.45) px`

イベント像の明点は球中心と一致しないため、絶対原点には上記予測値を使わない。
実行開始時は単に一定数のイベント重心を集めるだけではロックしない。直近0.25秒窓について
PAT X射影位置のrobust標準偏差、5--95 percentile幅、窓末尾と中央値の差を評価し、全条件が
0.25秒連続で安定した場合だけ、その直近窓のXY中央値を制御原点とする。既定条件は次のとおり。

- robust標準偏差（1.4826 x MAD）: 0.05 mm以下
- 5--95 percentile幅: 0.12 mm以下
- 窓末尾と中央値の差: 0.05 mm以下
- 安定待ちtimeout: 15秒

timeout時は補正を開始せず、中心トラップを保持して停止する。判定値はmanifestの
`lock_policy`と`lock_diagnostics`へ保存する。

過去のHFデータではイベント重心が概ね `(584, 477) px` だったが、2026-08-27の初回単眼RAWでは
現在の粒子イベントが概ね `(589, 390) px` にあった。現状態のROI候補は
`540,350,640,430` である。照明・粒子・カメラ姿勢が変わった場合は過去値をそのまま使わない。

## 制御周期

- 位置推定要求: 2,000 Hz
- イベントスライス: 500 us
- 移動平均: 4点、実時間2.0 ms
- OpenMPD要求フレームレート: 2,000 Hz

PATの40 kHzベースクロックや既存10 kHz軌道再生とは別である。閉ループ性能は、
PCから1 geometryを送る実測時間とジッタで判定する。

制御ループはOpenMPDのX位置を5 um刻みの事前計算lookup tableへ量子化する。
制御中にKD solverは実行しない。量子化位置が変わった場合だけ、事前変換済みの
1 geometryをOpenMPDへ送る。

## 安全制限

実機入口には次を固定または既定設定している。

- 左カメラserial固定: `00000508`
- X軸のみ
- 初回実機runは最大10秒
- 最大トラップ絶対オフセット: 0.20 mm、実機入口上限0.25 mm
- 最大トラップ－粒子相対変位: 0.30 mm
- 最大トラップ移動速度: 5 mm/s
- 20連続追跡欠落で停止
- 20連続制御周期超過で停止
- 位置安定性を満たさない場合は15秒でlock timeout停止
- 出力飽和、相対変位インターロック、slew limit
- 積分上限とconditional anti-windup

相対変位上限は指令を粒子側へ丸める制限ではない。現在送信中のトラップ、または次に要求する
トラップと粒子との距離が上限を超えた場合、新しいgeometryを送信せず、最後に送ったトラップを
保持して即時停止する。これにより、復元指令の符号が安全制限によって反転することを防ぐ。

異常停止時は、粒子位置を観測できないまま中心へ自動移動しない。最後に送ったトラップを
保持し、操作者が粒子を確保した後にEnterを押すとPATを停止・切断する。正常終了時だけ
中心ホログラムへ戻す。その後も粒子確保まではPATを保持する。

## 1. ハードウェアなし確認

小ゲインP制御:

```powershell
.\venv\Scripts\python.exe mono_eventcam_1axis_feedback.py `
  --mode simulate `
  --duration-sec 2 `
  --kp 0.05
```

論文モデルと論文PID値の確認:

```powershell
.\venv\Scripts\python.exe mono_eventcam_1axis_feedback.py `
  --mode simulate `
  --duration-sec 2 `
  --paper-gains `
  --max-trap-offset-mm 0.5 `
  --max-trap-particle-mm 0.5 `
  --max-slew-mm-s 1000 `
  --integral-output-limit-mm 0.5
```

シミュレーション成果物は`mono_feedback_records/mono_x_feedback_simulate_*`へ保存される。

## 2. 初回実機baseline

これはPATを中心保持し、単眼追跡と処理時間を記録する。`kp=0`なのでフィードバック補正は行わない。

```powershell
.\venv\Scripts\python.exe mono_eventcam_1axis_feedback.py `
  --mode hardware `
  --duration-sec 5 `
  --kp 0 `
  --max-trap-offset-mm 0.02 `
  --max-trap-particle-mm 0.30 `
  --max-slew-mm-s 0.5 `
  --tracking-roi 540,350,640,430 `
  --acknowledge-real-time-feedback-risk
```

実行中に2つの操作者確認がある。

1. `RUN_MONO_X`を入力して、lookup計算とOpenMPD接続を許可する。
2. 中心トラップ開始後、粒子と照明を確認してEnterを押し、左カメラを開く。

安定原点lockが成立しなければ補正は開始しない。`[LOCK WAIT]`のrobust標準偏差、p90幅、
endpoint値を確認する。ROIが不適切な場合は値を広げ続けず、
イベント像を確認して粒子だけを含むROIへ修正する。

## 3. 最初のP補正run

baselineの次は、符号とライブ送信を確認するため、5秒・最大20 um・Pのみで実行する。

```powershell
.\venv\Scripts\python.exe mono_eventcam_1axis_feedback.py `
  --mode hardware `
  --duration-sec 5 `
  --kp 0.01 `
  --integral-hz 0 `
  --derivative-hz 0 `
  --moving-average-samples 4 `
  --max-trap-offset-mm 0.02 `
  --max-trap-particle-mm 0.30 `
  --max-slew-mm-s 0.5 `
  --lookup-step-mm 0.0025 `
  --tracking-roi 540,350,640,430 `
  --acknowledge-real-time-feedback-risk
```

P制御だけでは減衰を増やせないため、baselineよりRMSが必ず改善するとは限らない。
この段階の合格条件は次のとおり。

- 粒子が画像上で正方向へずれたとき、トラップ指令が負方向になる
- `status=complete`、追跡欠落停止なし
- `valid_fraction >= 0.999`
- OpenMPD送信と全処理の95パーセンタイルが500 usを継続的に超えない
- トラップ指令が±0.02 mmへ張り付かない
- `relative_limit_exceeded=0`（1になった場合は停止理由と直前の位置を確認する）
- 粒子ロスト、隣接ノード遷移、他軸の目視上の異常振動がない

## 4. baselineとの比較

```powershell
.\venv\Scripts\python.exe mono_feedback_compare.py `
  mono_feedback_records\<BASELINE_RUN> `
  mono_feedback_records\<P_FEEDBACK_RUN>
```

`comparison.json`と`comparison.png`に、RMS、標準偏差、95%絶対誤差、最大誤差、
追跡有効率、OpenMPD送信時間、全処理時間を出力する。

2026-08-27の`comparison_20260827_143430`は、旧相対変位処理が復元指令を反転させたため
制御性能の判定には使用しない。追跡有効率と処理時間の確認には使用できる。

安全修正後の`mono_x_feedback_hardware_20260827_150510`と`...150730`は、新しいgeometryを
一度も送らず相対距離interlockで停止した。旧ロックが位置安定性を確認していなかったことが原因。
新しい安定ロックのRAWオフライン再生では、この2本はロック拒否され、以前の正常baseline
`...143059`は0.895秒でロックできることを確認済み。

## 5. PD/PIDへ進む条件

次の条件を満たすまでD/Iを有効にしない。

- baselineとP runがともに完走
- X方向の符号が正しい
- 追跡外れ値と送信deadline missが許容範囲
- 実測の総遅延または少なくともホスト送信遅延上限が得られた
- 粒子のX軸固有振動数と減衰比を既存データまたは今回RAWから同定できた

DまたはI、あるいは`kp>0.10`を実機で使うには
`--acknowledge-advanced-gains`も必要になる。論文値`kp=0.35, fi=23 Hz, fd=3.6 Hz`は、
本セットアップのプラント同定前には実機へ投入しない。

## 保存物

各実機runは`mono_feedback_records/mono_x_feedback_hardware_*`へ新規保存する。

```text
manifest.json
left_events.raw
control_log.csv
```

修正版の`control_log.csv`には、実送信トラップに加えて`unsaturated_trap_x_mm`、
`absolute_limited_trap_x_mm`、`requested_trap_x_mm`、現在/要求相対距離、`output_saturated`、
`relative_limit_exceeded`、`slew_limited`を保存する。

既存の`stereo_acoustools_3d_records_auto/`以下には書き込まない。

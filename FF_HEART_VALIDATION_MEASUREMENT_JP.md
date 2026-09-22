# ハート7 mm・10 HzのFF指令検証計測

作成: 2026-09-18

`G:\マイドライブ\Experiment\20260918\measurement_plan\ff_heart` にある事前計算済みの指令列
（OFF／A／C、および2026-09-18 21:40追加のOT-ident）を、現行のステレオ自動計測（`acoustools_stereo_eventcam_3d_recording_auto.py
--hf-export-dir`）で実機へ送り、これまでと同じ形式で記録できるようにしたものです。
計画書と生成器の写しは `ff_heart_20260918/REFERENCE_FF_HEART_README.md` と
`ff_heart_20260918/reference_make_ff_heart.py` にあります。

## 指令の中身

| run名 | 設計 | 指令 | max\|u−r\| | 予測追従誤差（rms） |
|---|---|---|---:|---:|
| `ffheart_s7_f10_OFF` | OFF | u = r（対照） | 0 mm | 0.53 mm |
| `ffheart_s7_f10_A_delay` | A | u(t) = r(t + 0.8 ms) | 0.596 mm | 0.19 mm |
| `ffheart_s7_f10_C_delay_inverse` | C | 先読み＋軸ごとの線形逆モデル (r̈ + γṙ)/k | 0.713 mm | 0.03 mm（理想値） |
| `ffheart_OT_ident_nodelay` | OT-ident | 遅延なしの線形逆モデル（実機同定のk・γ）。Cから先読みを除いたもの | 0.287 mm | 0.462 mm |
| `ffheart_A_delay03` | A03 | u(t) = r(t + 0.3 ms)。2026-09-18のステップで測った遅延に合わせ直したA | 0.224 mm | （計画書に記載なし） |
| `ffheart_C_delay03_inv_k918` | C03 | 先読み0.3 ms＋線形逆モデル（k x 0.25／z 3.45、γ x 0.050／z 0.022。2026-09-18のステップ由来）。設計名は `C_delay03_inverse_k918` | 0.455 mm | （計画書に記載なし） |
| `ffheart_s7_f5_OFF` | OFF5 | u = r。同じハートを周回5 Hzで描く（最大速度372 mm/s、最大加速度23,352 mm/s²）。2026-09-19追加 | 0 mm | （計画書に記載なし） |
| `ffheart_s7_f1_OFF` | OFF1 | u = r。周回1 Hz、**14 s**（最大速度74 mm/s、最大加速度934 mm/s²）。2026-09-19追加 | 0 mm | （計画書に記載なし） |
| `ffheart_s7_f2_OFF` | OFF2 | u = r。周回2 Hz、**14 s**（最大速度149 mm/s、最大加速度3,736 mm/s²）。2026-09-19追加 | 0 mm | （計画書に記載なし） |
| `ffheart_s7_f7_OFFW` | OFFW7 | u = r − w（1〜2 Hzで測った平衡点のずれ w を引く）。7 Hz。2026-09-20追加 | 0.564 mm | （計画書に記載なし） |
| `ffheart_s7_f7_CW` | CW7 | u = C − w。7 Hz。2026-09-20追加 | 0.834 mm | （計画書に記載なし） |
| `ffheart_s7_f7_C_delay_inv` | C7 | 設計Cの7 Hz版（CW7の比較相手）。設計名は `C_delay_inverse` | 0.479 mm | （計画書に記載なし） |
| `ffheart_s7_f10_OFFW` | OFFW10 | u = r − w。10 Hz。**加速度104,193 mm/s²で、このrunだけ上限を110,000へ上げてある** | 0.393 mm | （計画書に記載なし） |
| `ffheart_s7_f10_CW` | CW10 | u = C − w。10 Hz。**加速度106,815 mm/s²で、このrunだけ上限を110,000へ上げてある** | 0.886 mm | （計画書に記載なし） |
| `ffheart_s7_f7_OFFWxz` | OFFWXZ7 | u = r − w（面外yは補償しない）。7 Hz。2026-09-21追加 | 0.323 mm | （計画書に記載なし） |
| `ffheart_s7_f7_CWxz` | CWXZ7 | u = C − w（面外yは補償しない）。7 Hz。2026-09-21追加 | 0.698 mm | （計画書に記載なし） |
| `ffheart_s7_f7_OFFW2` | OFFW2 | u = r − w2（yの細かい成分を共振応答から逆算した版）。7 Hz。2026-09-21追加 | 0.465 mm | （計画書に記載なし） |
| `ffheart_s7_f7_CW2` | CW2 | u = C − w2。7 Hz。2026-09-21追加 | 0.770 mm | （計画書に記載なし） |
| `wscan_XZ_a` ほか10本 | WXZ／WYZ／WXY／WXZP4／WXZM4 | 外乱の場 w の面走査。渦巻き（中心→半径9.2 mm→中心、1.5回転/s、14 s、補償なし）。設計名は `WSCAN`。2026-09-20追加 | 0 mm | （計画書に記載なし） |
| `ffheart_s7_f1_OFF_yz` ほか | OFF1YZ／OFF2YZ／OFF5YZ／OFF7YZ／OFF10YZ | 上のOFF（1／2／5／7／10 Hz）と同じ指令を**YZ面**に描く。取得側で追加（下記） | 0 mm | （計画書に記載なし） |
| `ffheart_s7_f7_OFF` | OFF7 | u = r。同じハートを周回7 Hzで描く（最大速度521 mm/s、最大加速度45,770 mm/s²）。2026-09-19追加 | 0 mm | （計画書に記載なし） |

- 所望軌道 r はXZ面のハート（mode 4と同じ式）、scale 7 mm、10 Hz、8 s、10 kHz、両端0.5 sランプ。
  指令範囲は x ±7 mm、z −9.15〜+6.42 mm、最大速度745 mm/s、最大加速度93,401 mm/s²。
- YZ面のrun（run名の末尾が `_yz`）は計画書にはなく、取得側で足したものです。G:のXZ面の指令を数値そのまま取り込み、
  x列とy列を入れ替えるだけです（planの `swap_axes: ["x", "y"]`）。所望軌道 r も同じく入れ替わり、時間配列はそのままです。
  内容ハッシュの固定はXZ面と同じG:のファイルに対して行い、exportには入れ替え後の配列のハッシュを記録します。
  速度・加速度・オフセットの大きさはXZ面と同じなので、制限に対する余裕も同じです。設計名は `OFF10` が `OFF` と同じrunです。
- OFFWXZ7／CWXZ7／OFFW2／CW2（2026-09-21、`w_field/W_FIELD_3D_PLAN.md` の段階2）は、9/20の結果を受けた4本です。
  面内（x・z）の補償は効きました（xの共振帯 −63%、面内の再現誤差0.17 mm）が、面外のyは悪化しました
  （共振帯 0.17 → 0.74 mm）。ゆっくり回して測った w のyの細かい成分が、トラップの本当のずれではなく
  測定の偏りだったためです。`xz` の2本はyを補償せず、`2` の2本はyを共振応答から逆算した値に差し替えてあります。
  **4本は同じsessionで、休止 → kcheck の直後に続けて撮ってください**（熱状態をそろえるため）。
- w走査（`wscan_*`、2026-09-20、G:の `w_scan`）は、w を経路ごとではなく面として測るためのrunです（`NEXT_MEASUREMENTS_20260920.md` の2番）。
  最大87 mm/s・818 mm/s²と遅いので共振は立たず、粒子が w をそのままなぞります。a（反時計回り）とb（時計回り）を続けて撮ると、
  w が位置だけの関数か進む向きにも依るかを判定できます。剛性確認のステップは不要なので、設計名にw走査を選ぶと前後のkcheckは入りません。
  面は5つで、XZ（y=0）・YZ（x=0）・XY（z=0）が半径9.2 mm、y=±4 mmへずらしたXZが半径8 mm。±4 mmの2面は最初の0.5 sでyへ移り、
  最後に戻るので、どのrunも中心から始まって中心で終わります。
- OFFW／CW（2026-09-20、G:の `Experiment\20260919\measurement_plan\ff_heart`）は、1〜2 HzのOFFで測った外乱の場 w を指令から引いたものです。
  w は3次元なので、XZ面のハートでも指令にyの成分（最大0.465 mm）があります。w は面ごとに違うため、YZ面版は作っていません。
  計画書の本命は7 Hz（OFF7・C7と比べる。共振の位相が剛性で回るので休止直後に撮る）。10 Hz版は加速度が既定の上限100,000 mm/s²を
  4〜7%超えるので、計画書の指示どおりこの2本だけ `safety_limits` の加速度を110,000にしています（他の制限は既定のまま）。
- OFF1／OFF2は、共振で増幅されない速さで平衡点のずれ w(u) を直接なぞらせるためのrunです（計画書README）。
- OFF5／OFF7は、10 Hzのハートで見えた70〜90 Hzの残差の加振源を切り分けるための補償なしrunです
  （力の非線形が源なら周回を遅くすると消え、音場の空間リップルや位相の量子化が源なら共振帯に残る）。
  経路・大きさ・長さ（8 s）は10 HzのOFFと同じで、フォルダ名は `..._ffheart_s7_f5_OFF_scale100_<時刻>` になります。
  解析側の `evaluate_ff_heart.py` はフォルダ名の `_OFF_` で設計を判別し、周回周波数を10 Hz固定（`F = 10.0`）で
  扱っているので、この2本は解析側で周波数を切り替える必要があります（manifestの
  `feedforward_design.loop_frequency_hz` に5／7を記録しています）。
- OT-identはOptiTrap型の比較腕です（論文のモデルに遅延項はないため、Aではなくこちらが追試に当たります）。
  最大速度720 mm/s、最大加速度92,506 mm/s²で、既存の制限内です。計画書にはもう1本
  OT-paper（論文パラメータ、加速度98,822 mm/s²で上限100,000の98.8%）もありますが、取り込んでいません。
- 剛性確認用の短いステップも同じexportに入れてあります（サンドイッチ用）。

| run名 | 内容 | 時間 |
|---|---|---|
| `kcheck_x_S105_6jumps` | x: 0→+1.05→0→−1.05→0→+1.05→0 mm、保持2.0 s、6ジャンプ | 14 s |
| `kcheck_z_S105_6jumps` | z: 同上 | 14 s |
| `kcheck_y_S105_6jumps` | y: 同上（2026-09-19追加。export indexは012） | 14 s |

## 取り込みの方針（重要）

- ハート指令は `ff_heart_20260918/source/*.npz`（G:のファイルの写し）から
  **数値を一切変えずに**取り込みます（新設 `kind=imported`）。exportの指令・所望軌道・時間配列は
  G:のNPZとビット一致します（`ff_heart_20260918/reference_comparison_20260918.json`）。
- plan JSONは `positions_mm` のfloat64内容のSHA-256を固定しています。別のファイルに
  差し替わると生成時に止まります。実機読み込み時にもexportのNPZを同じハッシュで再検査します。
- 参照生成器をこのPCで実行し直すと、OS間の浮動小数点差で最大3e-10 mmだけ異なるため、
  ハッシュは一致しません。`source/` が無い環境では、再生成ではなくG:からコピーしてください。
- 取得側ではフィードフォワード・クリップ・縮小を**何も足しません**。制限を超える指令は
  生成時に例外で止まります。manifestの `delay_feedforward_applied=false` は
  「取得側で何も足していない」という意味で、A／Cの指令自体にはオフライン設計のFFが入っています
  （`trajectory_source.command_contains_offline_feedforward=true`、`feedforward_design` に設計値）。

安全制限（このplan専用）:

| 項目 | 制限 | 実際の最大 |
|---|---:|---:|
| `max_offset_mm` | 9.5 | 9.154（96%） |
| `max_speed_mm_s` | 800 | 745（93%） |
| `max_acceleration_mm_s2` | 100,000 | 93,401（93%） |
| `max_command_reference_distance_mm`（\|u−r\|） | 1.0 | 0.713（C） |
| `max_endpoint_offset_mm`（始点・終点の中心からの距離） | 0.01 | 0.0002（C） |

ステップ2本は実験ごとの `safety_limits` で1.05 mmちょうどに固定しています
（planの既定値を実験単位で上書きできるようにしました）。

## 一括実行

```powershell
# 実機を開かない確認（未生成ならexportもここで作られます）
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -DryRunOnly

# 実機計測（計画書の提案どおりの並び。最初のrunだけpreviewとEnter、以後は自動で続く）
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1

# 全runでpreviewとEnterを行う（2026-09-20より前の動作）
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -ConfirmEachRun

# 失敗時も尋ねず、自動で撮り直す
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Unattended
```

既定の順序は計画書の提案どおり **OFF → A → C → C → A → OFF** で、各ハートrunの前と
最後に x／y／z の剛性確認ステップを入れます（2026-09-19からyを追加。合計27 run、PAT再生のみで342 s。
従来のx／zだけにするには `-SandwichAxes "x,z"`）。

```
kx ky kz OFF kx ky kz A kx ky kz C kx ky kz C kx ky kz A kx ky kz OFF kx ky kz
```

主なオプション:

| オプション | 既定 | 意味 |
|---|---|---|
| `-Designs "OFF,A,C,C,A,OFF"` | 左記 | ハートrunの順序（OFF／A／C／OTI／A03／C03、OFF1／OFF2／OFF5／OFF7／OFF10、OFF1YZ／OFF2YZ／OFF5YZ／OFF7YZ／OFF10YZ の並び。繰り返し可。OTIは `OT-ident` とも書けます） |
| `-SandwichAxes "x,y,z"` | x,y,z | 剛性確認の軸（x／y／zから選ぶ）。`-NoSandwich` で省略 |
| `-CommandScale 0.5` | 1.0 | ハート指令だけを縮小。A／Cはrに対して線形なので、縮小しても同じ設計の小さいハートになります |
| `-ConfirmEachRun` | off | 全runでステレオpreviewとEnter（2026-09-20より前の動作） |
| `-Unattended` | off | 記録・LED検出の失敗時に尋ねず、`-AutomaticCaptureRetries`（既定2）回まで自動で撮り直す |
| `-CaptureTailMarginSec` | 5 | 記録末尾の余裕。140,000フレームのステップはPAT送信開始が約2 s遅れるため、既定の2 sでは足りません |
| `-OutputDir` | `.\stereo_acoustools_3d_records_ff_heart` | 出力先 |
| `-Regenerate` | off | exportを作り直す |
| `-KeepFailedCaptures` | off | 失敗した試行のファイル（LEDのイベント数の図、記録プロセスのmeta）を削除せず残す。原因調査用 |

面外yの扱いを比べる例（計画書の段階2。kcheckで挟むと4本で17 run。長いので2本ずつに分けてもよい）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OFFWXZ7,CWXZ7,OFFW2,CW2"
```

w の面走査を撮る例（XZ面のa→bで2 run、28 s。kcheckは入りません）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "WXZ"
```

計画書の順（XZ→YZ→XY→y=±4 mm。10 run・140 s）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "WXZ,WYZ,WXY,WXZP4,WXZM4"
```

w補償を7 Hzで撮る例（比較相手のOFF7・C7と並べて、x／y／zのステップで挟むと4本で17 run。長いので2回に分けてもよい）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OFF7,OFFW7,C7,CW7"
```

10 Hz版（加速度の上限を110,000へ上げたrun）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OFFW10,CW10"
```

周回1 Hz・2 Hzの補償なしを、XZ面とYZ面で撮る例（ハート2本をx／y／zのステップで挟むと11 run）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OFF1,OFF2"
```

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OFF1YZ,OFF2YZ"
```

以下の例のrun数は、剛性確認がx／zだけだったときの数です（今の既定x／y／zでは、ハート2本で11 run）。

OT-identを撮る例（計画書の推奨どおり2回、ステップで挟んで8 run。ハートだけなら
`-NoSandwich` を付けて2 run）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OTI,OTI"
```

周回5 Hz・7 Hzの補償なしを撮る例（ステップで挟んで8 run）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OFF5,OFF7"
```

遅延0.3 msに合わせ直した2本を撮る例（ステップで挟んで8 run）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "A03,C03"
```

planに設計を追加した後は、スクリプトが起動時に「exportがplanより古い」ことを検出して自動で作り直します
（planとexportが一致している間は書き換えません）。sessionの実行中にexportを手で作り直さないでください。

初めて実機へ送るときの軽い確認例（OFFだけ、半分の大きさ、ステップなし）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 `
  -Designs "OFF" -NoSandwich -CommandScale 0.5
```

注意:

- **run間の確認は既定で最初の1回だけです**（2026-09-20に変更）。2本目以降はpreviewもEnterもなく自動で始まります。
  記録やLED検出に失敗したときだけ「同じ軌道を再計測しますか？ (Y/n)」を出します。全runで確認したいときは
  `-ConfirmEachRun`、失敗時も尋ねず自動で撮り直すなら `-Unattended` です。
- **粒子の脱落は自動では検知しません。** 落ちた後のrunも空の記録として続きます。
- **記録プロセスが終了しない失敗が2026-09-18に2回起きています**（20:13のsessionの9本目のC、
  22:05のsessionの3本目のOT-ident。どちらもハートのrunで、ステップ20本では起きていません）。
  止まったのは左カメラ（Master）のイベント配信で、原因は未特定です。2026-09-18の改修後は、
  配信が3秒止まった時点で記録プロセスが原因を表示して失敗し、「同じ軌道を再計測しますか？ (Y/n)」と
  尋ねます（Enterで撮り直し。session全体は止まりません）。`-Unattended` では同じrunを最大2回まで
  自動で撮り直します。
- 1 sessionは短めに分けることを推奨します。2026-09-16には、同一PAT接続の16〜17分後（9〜10本目）でLED不検出と
  イベント激減が2回起きています（原因未特定）。13 run以上を選ぶとスクリプトが警告します。
  x／y／zのステップで挟む場合、ハート2本で11 runです（例 `-Designs "OFF1,OFF2"`）。
  8 runに分けた20:59と21:20のsessionはどちらも全run成功しました。
- 計画書は「ウォームアップ10分、または各run前に5分休止のどちらかに統一」を求めています。
  待ち時間はスクリプトでは入れていないので、運用で揃えてください。

## スクリプトを使わない場合

```powershell
# 生成（実機を開きません）
.\venv\Scripts\python.exe .\hf_identification_trajectory.py `
  --plan .\ff_heart_20260918\ff_heart_plan.json `
  --output-dir .\ff_heart_20260918\export_ff_heart

# ハードウェアを開かない確認
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\ff_heart_20260918\export_ff_heart --dry-run

# 実機: OFF → A → C を1本ずつ（この直接の呼び方では全runでpreviewとEnter。
# 最初の1本だけにするなら --unattended-after-first-checkpoint --prompt-on-capture-failure）
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\ff_heart_20260918\export_ff_heart `
  --output-dir .\stereo_acoustools_3d_records_ff_heart `
  --hf-run ffheart_s7_f10_OFF=1 `
  --hf-run ffheart_s7_f10_A_delay=1 `
  --hf-run ffheart_s7_f10_C_delay_inverse=1 `
  --capture-tail-margin-sec 5 `
  --acknowledge-ff-validation-protocol
```

`--hf-run LABEL=SCALE` は同じlabelを何度でも、書いた順に記録します。ステップ
（`kcheck_*`）を含める場合は `--acknowledge-step-response-risk` も必要です。
`--unattended-after-first-checkpoint` は、選択runがstep-response staircaseと
FF検証の取り込み指令だけのときに使えます（混在可）。`--keep-going` と、他の種類のexportとの
混在は拒否されます。`--acknowledge-hf-retention-and-visibility` は不要です。

## run名とフォルダ名の長さ

解析側のスクリプトは、runフォルダ名に `_<設計名>_`（例 `_C_delay_inverse_`）が含まれるかで
runを探します。一方、取得側はWindowsのパス長上限を守るため、長いフォルダ名の末尾をハッシュに
置き換えます。既定の出力先では「形状名＋番号＋run名＋scale」が63文字までです。

- `ffheart_s7_f10_C_delay_inverse` は66文字になるため、記録済みのC 2本（2026-09-18 21:03と21:06）の
  フォルダ名は `feedforward_validation_004_ffheart_s7_f10_C_delay_in_af5d3e6291_<時刻>` です。
  `_C_delay_inverse_` では見つからないので、解析側では `_C_delay_in` で探すか、
  `pipeline_manifest.json` の `automation.label` を使ってください。記録自体は正常です。
- 記録済みのOFF・A・Cの名前は変えていません（同じ設計のlabelをsession間でそろえるため）。
  今後Cを撮り直す場合も同じ短縮名のフォルダになります。
- 後から追加する設計は短い形式にします。OT-identは `ffheart_OT_ident_nodelay` で、
  フォルダ名に `_OT_ident_nodelay_` がそのまま残ります。
- `-OutputDir` を長いパスにすると再び短縮されます。短縮が起きる場合は、dry-runと実機開始前に
  `[AUTO][WARN]` で知らせます（完全なlabelは常に `pipeline_manifest.json` に残ります）。
- 2026-09-18の3 sessionが使ったexportは `ff_heart_20260918/export_ff_heart_used_20260918_2013_to_2134/`
  に退避してあります。ステップ、OFF、A、Cの指令ファイルは新しいexportとバイト単位で同一です。

## 出力

各runは他のHF計測と同じ形式で保存されます（`pipeline_manifest.json` は
`processing_status=pending`）。FF検証のrunでは次が加わります。

| ファイル | 内容 |
|---|---|
| `*_ideal_log.csv` | PATへ送った指令 u(t)（絶対座標、m） |
| `*_reference_log.csv` | 所望軌道 r(t)（同じ座標系・同じ時刻。manifestの `reference_log`） |
| `command_trajectory.npz` | `offset_mm`（指令）、`reference_mm`（所望軌道）、`time_pat_sec`、`time_cam_expected_sec` ほか |

ideal logは1 µmに丸められるので、厳密な指令値は `command_trajectory.npz` を使ってください。

## 評価について

- 成功の指標は「測定位置 − 所望軌道 r」です。`stereo_acoustools_3d_postprocess.py` の
  ideal比較は指令 u との比較なので、A／Cではそのままでは指標になりません。r との比較は
  後処理後に次のように実行できます。

```powershell
.\venv\Scripts\python.exe .\stereo_compare_ideal_3d.py `
  <RUN>\stereo_recording\stereo_3d\stereo_3d_points.npz `
  <RUN>\<run_base>_reference_log.csv `
  --output-dir <RUN>\reference_comparison_3d
```

- 計画書は、指令とカメラの時間軸の比のずれ（+182 ppm）を補正するため、
  `time_cam_expected_sec` の時刻で測定位置を読み、同じ行の r と比べることを求めています。
  この補正は取得側では行っていません（解析側の作業）。
- 成功基準は、追従誤差rmsが再現しない残差の床0.15〜0.25 mmに届くことです。
- 粒子抽出・3D三角測量はこのPCでは実行せず、従来どおりスパコンで行います。

## テスト

```powershell
.\venv\Scripts\python.exe -B -m unittest test_ff_heart_validation -v
```

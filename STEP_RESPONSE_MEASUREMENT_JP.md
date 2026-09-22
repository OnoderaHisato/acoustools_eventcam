# 階段状トラップジャンプ・ステレオ計測

更新: 2026-08-21

## 目的

PATの指令位置だけを瞬時に移動し、その後の一定指令区間で粒子が自由減衰する様子を、左右のイベントカメラで記録します。粒子を手で移動する計測ではありません。

`20260820/STEP_RESPONSE_MEASUREMENT_HANDOFF.md`の案(b)は、現在の`acoustools_stereo_eventcam_3d_recording_auto.py --hf-export-dir`がすでに実現しているため、この既存経路へstaircase生成と専用安全ロックを接続しています。計測後の粒子抽出は自動実行しません。

## 現行パイプラインへの対応

```text
step_response_identification_plan.json
  -> hf_identification_trajectory.py
  -> step_response_identification_export/
       command_trajectory.npz
       command_offset_log.csv
       trajectory_metadata.json
       trajectory_preview.png
  -> acoustools_stereo_eventcam_3d_recording_auto.py --hf-export-dir ...
  -> stereo_acoustools_3d_records_step_response/<run>/
       left/right events.npz
       PAT開始LED診断
       ideal_log.csv
       pipeline_manifest.json (processing_status=pending)
```

次の既存設定は変更していません。

- 左カメラ`00000508`（Master）
- 右カメラ`00000509`（Slave）
- PAT開始LEDは左、ROI `600,0,1280,180`
- ステレオ校正`stereo_checkerboard_calib_extrinsics_20260805/stereo_calibration_square7p12_extrinsics_final.npz`
- PAT更新レート10,000 Hz（40,000 Hzベースクロックのdivider 4）
- カメラ記録の軌道末尾余裕2.0秒

## ステップ固有の安全検査

staircaseは意図的な不連続指令です。1サンプル差分から求めた速度・加速度は非常に大きくなり、連続軌道用の速度・加速度制限では正しく評価できません。この計測だけは次を検査します。

- 浮揚中心からの最大指令オフセット
- 1回の最大ジャンプ幅
- 40 kHz、音速343 m/sから見積もった脱出境界2.144 mm未満であること

微分値を無視しているのではなく、不連続ステップに適した変位ベース検査へ置き換えています。各runのpreviewには、指令、力最大点約1.072 mm、推定脱出境界約2.144 mmを表示します。

## 計測プラン

| tier | 軸 | 振幅 | 最大ジャンプ | 1 run | run数 | 現在の状態 |
|---|---|---|---:|---:|---:|---|
| A | X/Y/Z | 0.25, 0.40 mm | 0.40 mm | 6.8 s | 3 | 2026-08-21計測・生存確認済み |
| B | X/Y/Z | 0.25, 0.50, 0.70 mm | 0.70 mm | 9.2 s | 6 | 2026-08-21計測・生存確認済み |
| C | X/Y/Z | 0.30, 0.60, 0.85, 1.05 mm | 1.05 mm | 11.6 s | 6 | 2026-08-21計測・生存確認済み |
| D | X/Y/Z | 0.70, 0.95, 1.15, 1.25 mm | 1.25 mm | 11.6 s | 6 | 2026-08-21計測・生存確認済み |
| E | X/Y/Z | 1.25, 1.40, 1.55, 1.70 mm | 1.70 mm | 18.0 s | 6 | 2026-08-21計測・生存確認済み |
| F | X/Y/Z | 1.80, 1.90 mm（両符号） | 1.90 mm | 10.0 s | 3 | E全run生存確認済み・計測可 |
| G | X/Y/Z・符号別 | 2.00, 2.05, 2.10 mm | 2.10 mm | 8.0 s | 6 | F全run生存確認後 |
| H | X/Y/Z・符号別 | 2.15, 2.20, 2.30 mm | 2.30 mm | 8.0 s | 6 | G全run生存確認後・意図的境界超過 |

各Tier A runは、最初に中心で2.0秒静止し、その後16回のジャンプと17個のholdを持ちます。各ターゲットの次は必ず中心へ戻り、最後も中心で終わります。最大0.40 mmは推定脱出境界の18.7%です。

## Tier Aの準備済み成果物

`step_response_identification_export/`に次の3条件を生成済みです。

- `A0_x_staircase_small`
- `A1_y_staircase_small`
- `A2_z_staircase_small`

再生成する場合は、既存成果物を上書きしないよう新しい出力先を指定してください。

```powershell
.\venv\Scripts\python.exe .\hf_identification_trajectory.py `
  --plan .\step_response_identification_plan.json `
  --output-dir .\step_response_identification_export_new `
  --write-csv
```

## ハードウェアを開かない確認

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_export `
  --preview-only

.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --dry-run
```

## 実機計測

最初のX条件だけを記録する場合:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label A0_x_staircase_small `
  --acknowledge-step-response-risk
```

X条件の生存を目視確認した後、Y/Zを順番に記録する場合:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label A1_y_staircase_small `
  --label A2_z_staircase_small `
  --acknowledge-step-response-risk
```

Tier Aの3条件を一度に選ぶこともできますが、最初の実機試行では上の2段階を推奨します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --acknowledge-step-response-risk
```

staircase計測では、`--no-preview`を指定しても各runのステレオプレビューとEnter確認が強制されます。粒子が中心で安定していない、両カメラに見えない、または直前のrunで逸脱した場合はEnterを押さず中断してください。`--keep-going`は使用できません。

LED検出に失敗した場合は、現行recording coreの通常動作どおり、その試行を破棄して同じ計測を再取得するか確認されます。成功したrunだけが`processing_status=pending`で確定します。

## Tier B以降への進み方

Tier AのA0/X、A1/Y、A2/Zは2026-08-21に計測され、全48ジャンプ後に粒子が左右両眼で
見えていることを軽量イベント像で確認しました。左右同期、LED検出、記録区間にも異常は
ありません。これは生存・可視性の確認であり、粒子追跡や3D応答解析はまだ行っていません。

元のmaster planでは安全のためTier B/Cを`enabled: false`のまま保持しています。Tier Bだけを
有効にした次の専用成果物を追加しました。

```text
step_response_identification_tier_b_plan.json
step_response_identification_tier_b_export/
```

Tier BはX/Y/Z各2 run、合計6 runです。各runは9.2秒、中心静止2.0秒、24ジャンプ、
最大0.70 mmで、推定脱出境界2.144 mmの32.7%です。まずX軸の1 runだけを記録します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_b_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label B0_x_staircase_ladder_r01 `
  --acknowledge-step-response-risk
```

このrunの全ジャンプで生存・両眼可視を確認した後だけ、残り5 runを実行します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_b_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label B0_x_staircase_ladder_r02 `
  --label B1_y_staircase_ladder_r01 `
  --label B1_y_staircase_ladder_r02 `
  --label B2_z_staircase_ladder_r01 `
  --label B2_z_staircase_ladder_r02 `
  --acknowledge-step-response-risk
```

実機前の全6 run dry-runは次です。ハードウェアを開きません。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_b_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --dry-run
```

Tier B以降も実機入口で必要なのは`--acknowledge-step-response-risk`だけです（前Tier生存確認フラグは2026-09-16に廃止）。

Tier Bの6 runは2026-08-21に計測され、全144ジャンプ後に左右両眼で粒子が見えていることを
確認しました。6 runとも左右同期、LED検出、記録区間、保存状態は正常です。したがってTier Cへ
進めます。Tier Cだけを持つ次の専用成果物を準備済みです。

```text
step_response_identification_tier_c_plan.json
step_response_identification_tier_c_export/
```

Tier CはX/Y/Z各2 run、合計6 runです。各runは11.6秒、32ジャンプ、最大1.05 mmで、
推定脱出境界の49.0%です。全6 runを1コマンドで選択できます。実機を開かないdry-run:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_c_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --dry-run
```

全6 runを同じセッションで実測するコマンド:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_c_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --acknowledge-step-response-risk
```

1コマンドですが、各run直前のステレオプレビューとEnter確認は強制されます。直前runで粒子が
逸脱した場合や両眼で見えない場合は、次のEnterを押さずに中断してください。`--keep-going`は
指定できません。

## Tier D（C確認後のみ）

Tier Cが余裕をもって完了した場合に備え、挑戦的なTier Dも準備済みです。

```text
step_response_identification_tier_d_plan.json
step_response_identification_tier_d_export/
```

Tier Dは`±0.70, ±0.95, ±1.15, ±1.25 mm`です。1.15/1.25 mmは推定力最大点約1.072 mmを
越え、復元力が低下し始める領域に入るため、Tier Cより明確に高リスクです。一方、最大1.25 mmは
推定脱出境界2.144 mmの58.3%に制限しています。これは保持を保証する値ではありません。

Tier Cの全192ジャンプ（32ジャンプ×6 run）で生存・両眼可視を確認するまでは実測しないで
ください。前Tierの生存確認はコードでは強制されないため、運用で守ってください。

全6条件のhardware-free dry-runは可能です。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_d_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --dry-run
```

Tier C確認後に、最初のD0/X r01だけを実測するコマンド:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_d_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label D0_x_staircase_challenge_r01 `
  --acknowledge-step-response-risk
```

D0/X r01は2026-08-21に計測済みで、全32ジャンプ後の左右粒子像、同期、LED、記録完走を
確認済みです。残り5 runは1回のhardware sessionで連続計測できます。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_d_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label D0_x_staircase_challenge_r02 `
  --label D1_y_staircase_challenge_r01 `
  --label D1_y_staircase_challenge_r02 `
  --label D2_z_staircase_challenge_r01 `
  --label D2_z_staircase_challenge_r02 `
  --acknowledge-step-response-risk
```

PAT/OpenMPD接続は5 runの間維持されますが、各runの前にはステレオプレビューとEnter確認が
必ず入ります。前runで粒子が逸脱した、中心に戻っていない、または片眼で見えない場合はEnterを
押さずに中断してください。`--keep-going`は禁止のままで、拒否・記録失敗・Ctrl+Cでは後続runを
実行せず、共通終了処理でPATを停止します。

## Tier E（長保持・D確認後のみ）

減衰の遅い尾部まで観察するため、Tier Eでは従来の0.3秒保持を1.0秒へ延長しました。
ターゲット位置を1.0秒保持した後に中心へ戻し、中心でも1.0秒保持するため、すべての
ジャンプ間隔が正確に1.0秒です。外向きジャンプと中心復帰ジャンプの両方で、次の指令に
重なる前の減衰を観測できます。

```text
step_response_identification_tier_e_plan.json
step_response_identification_tier_e_export/
```

Tier Eは`±1.25, ±1.40, ±1.55, ±1.70 mm`で、全振幅が推定力最大点約1.072 mmを越えます。
最大1.70 mmは推定脱出境界2.144 mmの79.3%であり、保持限界に近い高リスク診断です。
推定境界内であることは保持成功を保証しません。

近境界ジャンプの曝露回数を抑えるため、各run内では各符号付き振幅を1回だけ測ります。
その結果、1 runは初期中心2.0秒、16ジャンプ、17 hold、合計18.0秒です。独立反復は従来どおり
`r01/r02`の2 runで確保します。

Tier Dの全192ジャンプ（32ジャンプ×6 run）と、Tier Eの最初のE0/X r01全16ジャンプで
生存・両眼可視を確認済みです。Tier Eの複数runを同じhardware sessionで連続計測できます。

全6条件のhardware-free dry-run:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_e_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --dry-run
```

Tier D確認後に、最初のE0/X r01だけを実測するコマンド:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_e_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label E0_x_staircase_boundary_long_hold_r01 `
  --acknowledge-step-response-risk
```

E0/X r01は2026-08-21に計測済みで、全16ジャンプの直後5--35 msと保持後半850--900 msに
左右両眼で粒子像を確認しました。左右同期、LED検出、20.5秒の記録区間、NPZ保存も正常です。
残り5 runを1回のhardware sessionで連続計測するコマンド:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_e_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label E0_x_staircase_boundary_long_hold_r02 `
  --label E1_y_staircase_boundary_long_hold_r01 `
  --label E1_y_staircase_boundary_long_hold_r02 `
  --label E2_z_staircase_boundary_long_hold_r01 `
  --label E2_z_staircase_boundary_long_hold_r02 `
  --acknowledge-step-response-risk
```

各run直前のステレオプレビューとEnter確認は強制されます。前runで粒子が逸脱した、中心に
戻っていない、または片眼で見えない場合はEnterを押さずに中断してください。`--keep-going`は
禁止のままで、拒否・記録失敗・Ctrl+Cでは後続runを実行せず、共通終了処理でPATを停止します。

## 確認フラグ（2026-09-16改訂）

前Tierの生存確認フラグ`--acknowledge-step-response-tier-a-survived`〜`--acknowledge-step-response-tier-g-survived`は廃止しました。現在は指定するとargparseのエラーになります。staircase実機計測で必要なのは次だけです。

```text
--acknowledge-step-response-risk                    # 全Tier共通
--acknowledge-step-response-escape-boundary-probe   # Tier Hのみ追加
```

前Tierの生存確認は、各run直前に強制されるステレオプレビューとEnter確認で行ってください。確認前に次Tierを実測したり、異なるTierを同じexportへ混在させたりしないでください。

## Tier F/G/H（保持限界の探索）

Tier Eより先は、推定脱出境界2.144 mmそのものを探索する診断です。Tier Fは境界の88.6%、
Tier Gは98.0%までで、Tier Hは最初の2.15 mmから境界を越えます。Tier Hでは粒子脱落または
隣接ノードへの移動を正常な診断結果として想定します。

```text
step_response_identification_tier_f_plan.json / _export/
step_response_identification_tier_g_plan.json / _export/
step_response_identification_tier_h_plan.json / _export/
```

Fは各軸1 runで、`+1.80, -1.80, +1.90, -1.90 mm`の順に中心との往復を行います。G/Hは
軸と符号を別runにし、各run内では振幅を弱い順に上げます。Hはコード側で1 hardware session
につき1 runだけに制限し、通常のstep-risk確認に加えて境界超過専用ackを要求します。

実行順、確認フラグ、各コマンドは`STEP_RESPONSE_ESCAPE_BOUNDARY_JP.md`を参照してください。

各Tierのexportを別名で再生成する場合は、既存の検証済みexportを上書きしないでください。

```powershell
.\venv\Scripts\python.exe .\hf_identification_trajectory.py `
  --plan .\step_response_identification_tier_b_plan.json `
  --output-dir .\step_response_identification_tier_b_export_new `
  --write-csv

.\venv\Scripts\python.exe .\hf_identification_trajectory.py `
  --plan .\step_response_identification_tier_c_plan.json `
  --output-dir .\step_response_identification_tier_c_export_new `
  --write-csv

.\venv\Scripts\python.exe .\hf_identification_trajectory.py `
  --plan .\step_response_identification_tier_d_plan.json `
  --output-dir .\step_response_identification_tier_d_export_new `
  --write-csv

.\venv\Scripts\python.exe .\hf_identification_trajectory.py `
  --plan .\step_response_identification_tier_e_plan.json `
  --output-dir .\step_response_identification_tier_e_export_new `
  --write-csv
```

## 計測後

今回は粒子抽出を行いません。次だけ確認します。

- `pipeline_manifest.json`の`capture_complete=true`
- `processing_status=pending`
- 左右の`left_events.npz` / `right_events.npz`が存在する
- PAT開始LED検出結果が存在する
- `ideal_log.csv`とコピー済み`command_trajectory.npz`が存在する

これらを確認後、計測データを解析環境へ移します。

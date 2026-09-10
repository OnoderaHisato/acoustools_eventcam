# ステップ応答・保持限界探索 Tier F/G/H

更新: 2026-08-21

## 目的と前提

Tier Eの最大1.70 mmより先で、粒子が保持されなくなる指令ジャンプを軸・符号別に調べます。
40 kHz、音速343 m/sの単純化した復元力モデルによる推定脱出境界は2.144 mmです。これは実機の
保証値ではなく、粒子、音場、重力、軸方向、温度などにより実際の限界は前後します。

Tier Eの全6 runで粒子生存と左右可視性を確認するまでTier Fを開始しないでください。粒子が
飛んだ、隣のノードへ移った、中心へ戻らない、片眼で見えない場合は次のrunへ進みません。

## 段階

| Tier | 指令振幅 | 境界比 | run構成 | 意図 |
|---|---:|---:|---|---|
| F | 1.80, 1.90 mm | 最大88.6% | X/Y/Z、両符号を各run内で弱い順 | 境界手前への移行確認 |
| G | 2.00, 2.05, 2.10 mm | 最大98.0% | X/Y/Z×正負を分離 | 境界直前の方向別限界 |
| H | 2.15, 2.20, 2.30 mm | 100.3–107.3% | X/Y/Z×正負を分離 | 意図的な境界超過・脱落点探索 |

各ターゲットは1.0秒保持し、中心へ戻して1.0秒保持します。G/Hを符号別に分けたのは、最初の
脱落で反対符号の情報まで失うことを避け、方向非対称性を識別するためです。

## ハードウェアなし確認

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_f_export `
  --dry-run

.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_g_export `
  --dry-run

.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_h_export `
  --dry-run
```

## Tier F

E全runの生存確認後、まずXだけを実行します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_f_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label F0_x_staircase_near_escape `
  --acknowledge-step-response-risk `
  --acknowledge-step-response-tier-e-survived
```

Xの全8ジャンプで生存を確認後、Y/Zは次で選択できます。各run前のプレビュー・Enter確認は
強制されます。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_f_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label F1_y_staircase_near_escape `
  --label F2_z_staircase_near_escape `
  --acknowledge-step-response-risk `
  --acknowledge-step-response-tier-e-survived
```

## Tier G

FのX/Y/Z全runが生存した後だけ開始します。次のlabelを上から1本ずつ指定してください。

```text
G0_x_positive_staircase_escape_edge
G1_x_negative_staircase_escape_edge
G2_y_positive_staircase_escape_edge
G3_y_negative_staircase_escape_edge
G4_z_positive_staircase_escape_edge
G5_z_negative_staircase_escape_edge
```

例（最初のX正方向）:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_g_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label G0_x_positive_staircase_escape_edge `
  --acknowledge-step-response-risk `
  --acknowledge-step-response-tier-f-survived
```

各run内は`2.00 → center → 2.05 → center → 2.10 → center mm`です。負方向runでは符号だけが
反転します。脱落しなかった最大値と、脱落した最小値の間が、その軸・符号の保持限界区間です。

## Tier H：意図的な境界超過

Gの6方向すべてが2.10 mmまで生存した場合だけ実行します。粒子脱落または隣接ノードへの移動を
想定する破壊的診断です。コード側で複数labelを拒否するため、必ず1コマンド1 runです。

```text
H0_x_positive_staircase_escape_crossing
H1_x_negative_staircase_escape_crossing
H2_y_positive_staircase_escape_crossing
H3_y_negative_staircase_escape_crossing
H4_z_positive_staircase_escape_crossing
H5_z_negative_staircase_escape_crossing
```

例（最初のX正方向）:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_identification_tier_h_export `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --label H0_x_positive_staircase_escape_crossing `
  --acknowledge-step-response-risk `
  --acknowledge-step-response-tier-g-survived `
  --acknowledge-step-response-escape-boundary-probe
```

Hの専用ackは、2.15/2.20/2.30 mmが推定境界以上であり、再浮揚が必要になり得ることへの確認です。
一般のstaircaseへ境界超過を開放したわけではなく、Tier Hだけが最大2.30 mmまで許可されます。

## 判定

各run後は少なくとも次を確認します。

- `capture_complete=true`、左右同期、LED検出、全記録区間の保存
- 各外向きジャンプ直後と1秒保持後半に、粒子が左右両眼で同じノードに残っているか
- 中心復帰後に元の中心へ戻ったか
- 最後に生存した振幅と、最初に脱落・ノード移動した振幅

限界をさらに細かくする場合は、この粗探索後に生存値と脱落値の中点を追加します。最初から細かい
刻みを多数流すと近境界曝露を増やすため、F/G/Hではまず0.05–0.10 mm刻みの区間同定を優先します。

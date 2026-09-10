# B1 補償軌道 ON/OFF 計測・3D解析結果（2026-09-02）

## 結論

- 代表ランダム軌道 `balanced_xyz_8s_seed2101` では、逆2次モデル補償ONによりベクトルRMS誤差が `0.4854 mm` から `0.3617 mm` へ低下し、`25.5%` 改善した。x/y/z各軸もそれぞれ `18.3% / 28.0% / 34.9%` 改善した。
- ハート軌道（7 mm、10 Hz、1 s、10周回）では、ベクトルRMS誤差がペア1で `0.5054 -> 0.5351 mm`（`5.9%`悪化）、ペア2で `0.5217 -> 0.5668 mm`（`8.6%`悪化）となった。今回の10 Hzハート条件では、ONで理想軌道に近づいたとは判定できない。
- したがって、補償効果は今回の代表ランダム軌道では確認できたが、高調波を含む10 Hzハート軌道へはそのまま一般化できない。

## 処理内容と品質

- 対象は2026-09-02に再収録したハート4 run（OFF/ON 2ペア）と代表ランダム2 run（OFF/ON 1ペア）。NumPy scalarによる静止トラップ不具合があった2026-09-01のON収録は使用していない。
- 左右12個のイベントNPZから粒子を抽出した。各カメラのraw tracking valid率は全runで100%。
- ステレオ3D有効点はハート各run `35,000 / 35,000`、ランダムOFF `105,000 / 105,000`、ランダムON `104,997 / 105,000`。
- 全6 runについて `ideal_comparison_3d/stereo_ideal_comparison.npz`、CSV、summary JSON、比較図を生成した。
- 理想比較は各runの剛体fit（`spatial_alignment=fit-run`）であり、絶対姿勢ではなく軌道形状の一致度を評価する。

## ON/OFF比較

### 代表ランダム軌道

評価窓は `t >= 0.5 s`。OFF/ONとも、観測軌道と元の理想軌道 `r` の差で評価し、ON側の補償指令 `u` は評価基準に使用していない。

| 指標 | OFF RMS [mm] | ON RMS [mm] | 改善率 |
|--|--:|--:|--:|
| x | 0.2943 | 0.2405 | +18.3% |
| y | 0.3228 | 0.2325 | +28.0% |
| z | 0.2117 | 0.1378 | +34.9% |
| ベクトル | 0.4854 | 0.3617 | +25.5% |

最大ベクトル誤差はOFF `1.9294 mm`、ON `1.0052 mm`。どちらも解析上の脱出疑い閾値 `2 mm` を超えていない。

### ハート軌道

評価窓は初期過渡を除く `t >= 0.15 s`。

| ペア | OFF vector RMS [mm] | ON vector RMS [mm] | 改善率 | 判定 |
|--:|--:|--:|--:|--|
| 1 | 0.5054 | 0.5351 | -5.9% | 悪化 |
| 2 | 0.5217 | 0.5668 | -8.6% | 悪化 |

軸別には一部改善（ペア1のy、ペア2のz）があるが、ベクトルRMSは両ペアとも悪化した。

## 主要成果物

- ハート集計: `stereo_b1_heart_ab_records/b1_heart_analysis/HEART_AB_SUMMARY.md`
- ハート重ね描き: `stereo_b1_heart_ab_records/b1_heart_analysis/heart_xz_off_on.png`
- ランダム集計: `stereo_b1_ff_ab_records/b1_ab_analysis/B1_AB_SUMMARY.md`
- ランダム数値: `stereo_b1_ff_ab_records/b1_ab_analysis/per_run_metrics.csv`、`paired_improvements.csv`
- ランダム重ね描き: `stereo_b1_ff_ab_records/b1_ab_analysis/balanced_xyz_8s_seed2101_p01_full.png`
- 各runの3D点群: `stereo_recording/stereo_3d/stereo_3d_points.npz`
- 各runの理想比較: `ideal_comparison_3d/stereo_ideal_comparison.npz`

## セッション

- ハート: `stereo_b1_heart_ab_records/b1_heart_ab_session_20260902_003318.json`
- ランダム: `stereo_b1_ff_ab_records/b1_ff_ab_session_20260902_003551.json`

## Dドライブ保存

- 保存先: `D:\measurement_control_b1_20260902_results`
- 6 runの左右イベントNPZを含む生データ、粒子抽出、3D、理想比較、A/B集計、解析コードを保存した。
- コピー対象323ファイル、2,928,426,158 bytesについて、欠落0、サイズ不一致0、SHA-256不一致0を確認した。
- 全ファイルの相対パス、byte数、SHA-256は `TRANSFER_INVENTORY_SHA256.csv` に記録した。

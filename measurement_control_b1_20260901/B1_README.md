# B1: 実機前置補正 ON/OFF 試験（long_random_3d × 設計 C）

固定物理モデル ẍ = −ω0²(x − u(t−τ)) − γẋ の**逆モデル前置補正**が実機で効くかを、
同一セッション内の OFF/ON 交互収録で検証する。オフライン模擬（2026-08-27）の予測は
改善率 x/y/z ≈ +26〜29 / +28〜32 / +43〜49 %（加法外乱仮定の条件付き上限）。

- **OFF（baseline）**: u = r。2026-08-07 の auto セッションと完全に同じ。
- **ON（inverse2nd, 設計 C）**: 軸別に
  `u = r(t+τ) + (r̈ + γ ṙ)(t+τ) / ω0²`、|u−r| ≤ 1.0 mm（ベクトルノルム、λ/8=1.07mm に対する安全余裕）。
  物理値は ringdown 同定値: f0 = 81.2/84.2/270.0 Hz, γ = 31.4/36.2/23.8 1/s, τ = 2.0/2.1/1.8 ms。
- **ゼロショット**: これらの run で f0/γ/τ を再フィットしない。
- 評価は両条件とも `measured − r`（`*_ideal_log.csv` は OFF/ON どちらも元の参照 r(t) を保持。
  ON の PAT 指令 u は `*_command_log.csv` に別途保存される）。

## ファイル構成（実験 PC `C:\Users\digit\Documents\scripts\python\eventcam_control\` に全部コピー）

| ファイル | 内容 |
|--|--|
| `inverse2nd_feedforward.py` | 設計 C 補正モジュール（numpy のみ。SG 微分は scipy と 1e-12 mm 一致を検証済み） |
| `stereo_acoustools_b1_recording_core.py` | 収録コアのコピー + `--inverse2nd-feedforward` モード追加。**既存ファイルは無変更** |
| `acoustools_stereo_b1_ff_ab.py` | セッションドライバ（JSON 条件読み込み + OFF/ON カウンターバランス交互実行） |
| `delay_feedforward.py` | 収録コアが import する既存の一次遅れ補正モジュール（実験 PC に無い場合に備えて同梱。既にあれば上書き不要） |
| `stereo_acoustools_b1_heart_recording_core.py` | ハート用収録コア（`stereo_acoustools_heart_delay_recording_core.py` のコピー + inverse2nd モード、周期軌道対応） |
| `acoustools_stereo_b1_heart_ab.py` | ハート軌道の OFF/ON セッションドライバ（小規模な見た目検証用） |
| `compare_b1_ff_ab.py` | 事後解析（ステレオ後処理完了後にペア比較。**ローカル Mac 側で実行、実験 PC 不要**） |
| `validate_b1_feedforward.py` | ローカル検証（`Acoutools_eventcam/` のデータと `feedforward_inverse_sim.py` が必要。**実験 PC では動かない/不要**） |

既存モジュール（`stereo_acoustools_3d_auto_common.py`, `stereo_acoustools_3d_common.py`,
`acoustools_random3d_no_eventcam.py` 等）と
`long_random_3d_patterns_initial.json` がそのまま使われる。
後処理の `run_stereo3d_postprocess_miyabi.sh` も従来どおり**ローカル Mac から** Miyabi に対して実行するもので、実験 PC には置かない。

## ⚠️ 2026-09-01 の ON 収録は無効 → 修正版で再収録

2026-09-01 の小規模セッションで **ON 3 本すべてが静的トラップ**（粒子がアレイ中心に
静止したまま）になった。原因は AcousTools `create_points` が np.float64 座標を型チェックで
黙って捨て (0,0,0) にするため。`inverse2nd_feedforward.py` は builtin float を返すよう
修正済み（2026-09-01）。**修正版の `inverse2nd_feedforward.py` を実験 PC に上書きコピー**
してから下の小規模セッションを再実行すること。このバグはドライランでは検出できない
（指令 CSV・PAT 送信・LED すべて正常に見える）。OFF 収録は有効なので再収録不要だが、
セッション内ドリフト相殺のためペアごと（OFF/ON 両方）撮り直すのが安全。

## 小規模検証セッション（まずはこれ。48 本のフル統計版は不要）

「補正 ON できれいに映るか」を見るだけなら **6 収録 ≈ 15–20 分** で足りる:

```powershell
# (a) ハート軌道 OFF/ON × 2 ペア = 4 収録（1 run ≈ 1 s の軌道 + 撮影オーバーヘッド）
.\venv\Scripts\python.exe .\acoustools_stereo_b1_heart_ab.py --dry-run   # まず補正量確認
.\venv\Scripts\python.exe .\acoustools_stereo_b1_heart_ab.py --repeats 2

# (b) 代表ランダム軌道 1 本 OFF/ON × 1 ペア = 2 収録
.\venv\Scripts\python.exe .\acoustools_stereo_b1_ff_ab.py --label balanced_xyz_8s_seed2101 --pairs 1
```

- ハートは既定 7 mm・10 Hz（前回の delay A/B と同条件）。合成ハートでのローカル見積りでは
  10 Hz だと補正の生値が最大 ≈1.4 mm で **約 44 % の点が 1 mm クリップにかかる**が、
  クリップ込みでもモデル上の追従誤差は x +75 % / z +83 % 改善する予測
  （正確な数字は実機ドライランの PREFLIGHT 表示が正）。クリップ無しで試したい場合は
  `--heart-frequency-hz 5`（最大 ≈0.7 mm、クリップ 0 %）。
- ハートの評価・可視化は従来の heart_delay A/B と同じ（ideal_log = 元ハート r(t)、
  後処理後に測定軌道を重ねれば「きれいに映るか」がそのまま見える）。
- 効果が見えたら、統計を取りたくなった時点でフル版（下記 8 軌道 × 3 ペア = 48 収録）を回す。

## フル統計版の手順

### 1. ドライラン（ハードウェア無し）

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_b1_ff_ab.py --dry-run
```

8 条件それぞれの補正量が表示される。目安（ローカル検証値）:
max |u−r| は 0.44〜1.00 mm、クリップ率は 006_chirp_dense のみ 12.4 %（他は ≤2 %）。
表示される u の速度・加速度が r と同程度（速度はほぼ不変、加速度は数％増）であることを確認。
※ローカル検証で出た「u の加速度が桁で増える」数字は、補間再構成した r の区分線形
アーティファクトであり、実機の解析的に滑らかな r では出ない。ドライランの実測値が正。

### 2. 本番（8 軌道 × OFF/ON × 3 ペア = 48 収録、~1.5 時間）

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_b1_ff_ab.py --pairs 3
```

- 実行順はペア内 OFF/ON をペア番号×条件番号でカウンターバランス
  （条件0: OFF→ON / ON→OFF / OFF→ON、条件1 は逆から）。ドリフトの系統誤差をペア内で相殺。
- 1 ペアの OFF と ON は隣接収録（間隔 ~2 分）なので、セッション内ドリフト
  （平衡位置 1.5mm 級 / f0 の 70→85Hz 変動）はペア差分でほぼ消える。
- 時間が無い場合は `--pairs 1`（16 収録、~30 分）でも主効果は見える。
- 特定軌道のみ: `--label balanced_xyz_8s_seed2101` （repeatable）。
- 003 (vertical_rich, seed2404) も**収録には含める**（前回の欠損は左カメラの瞬間的
  ブロブ検出ロストで軌道自体の問題ではない）。解析時に欠損があれば coverage で分かる。
- 粒子が脱出したら: そのままセッション続行（`--keep-going`）または中断→粒子再設置→
  `--label`/`--start-index` で残りを再実行。脱出した run はペアごと解析から落とすか
  「脱出回数」として別途報告する（脱出も OFF vs ON の副次指標）。
- 出力: `stereo_b1_ff_ab_records/`（run ディレクトリ群 + `b1_ff_ab_session_*.json`）。
  run ディレクトリ名は `long_random_3d_{index}_{label}_p{ペア}_{off|on}[_inv2ff]_{時刻}`。

### 3. 後処理（従来どおり）

各 run の三角測量→ ideal 比較は従来の Miyabi チェーン
（`run_stereo3d_postprocess_miyabi.sh` 相当。ベースディレクトリを
`stereo_b1_ff_ab_records` に向ける）をそのまま使う。連続ランダム軌道なので
時刻同期は自動探索で収束する（階段応答の pitfall #12 は該当しない）。

### 4. 集計

後処理済み run ディレクトリと session JSON を同じ場所に置き:

```bash
python3 compare_b1_ff_ab.py stereo_b1_ff_ab_records/b1_ff_ab_session_YYYYMMDD_HHMMSS.json
```

出力: `b1_ab_analysis/{per_run_metrics.csv, paired_improvements.csv, summary.json, B1_AB_SUMMARY.md, paired_rms.png}`。
指標は t ≥ 0.5 s の軸別 rms(m−r) と改善率（ペア平均・中央値・最悪ペア・改善ペア数）、
脱出疑い数（|e| ベクトル > 2mm）。事前予測との比較列付き。

## 事前予測（ローカル線形重ね合わせ、run 別）

| 軌道 | max\|u−r\| [mm] | クリップ率 | 予測改善 x/y/z [%] |
|--|--:|--:|--|
| 000 balanced_xyz_8s | 1.00 | 0 % | +20 / +29 / +53 |
| 001 wide_xyz_10s | 1.00 | 0.04 % | +28 / +27 / +49 |
| 002 depth_rich_10s | 0.62 | 0 % | +14 / +23 / +35 |
| 004 high_frequency_8s | 1.00 | 2.1 % | +38 / +31 / +51 |
| 005 slow_drift_15s | 0.44 | 0 % | +17 / +9 / +27 |
| 006 chirp_dense_12s | 1.00 | 12.4 % | +42 / +44 / +62 |
| 007 full_range_10s | 0.94 | 0 % | +20 / +25 / +47 |
| **pooled** | | | **+29 / +32 / +49** |

## 注意・オプション

- クリップ方式のデフォルトは `pointwise_clip`（オフライン予測と同一条件）。
  006 のクリップ率が気になる場合は `--ff-limit-strategy global_scale`
  （滑らかだが補正が全体に弱まる）を**別セッション**で比較。混ぜない。
- `--ff-f0-hz/--ff-gamma/--ff-tau-ms/--ff-max-offset-mm` で物理値を変えられるが、
  ゼロショット主張のためデフォルト（ringdown 値）から動かさない。
- 評価は per-run Kabsch rigid-fit（形状比較）。OFF/ON 各 run が独自にフィットされるが、
  改善幅 26〜49 % に対しアライメント差は二次的。厳密にやるなら
  `stereo_fit_global_camera_to_pat.py` の固定変換で再評価して頑健性チェック。
- auto（連続ランダム）と step（階段）のデータを混ぜて集計しない。

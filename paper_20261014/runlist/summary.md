# 撮る順の一覧の集計（make_paper_runlist.py が作る。手で書き換えない）

見積もり: 1 本あたり 記録の長さ ＋ 65 s、粒子の交換 3 分。各セッションの頭に暖機 40 分（表の時間には入れていない）。

## S1: 115 本、145 分（暖機を足して 3.1 時間 = 3 時間 5 分）

| 段 | 粒子 | 本数 | うち kcheck・hold | 時間 [分] | 中身 |
|---|---|---|---|---|---|
| A1 | 0 | 4 | 4 | 5.2 | kcheck・hold |
| A2 | 0 | 2 | 0 | 3.8 | `wscan_XZ_R28d_a`、`wscan_XZ_R28d_b` |
| A3 | 0 | 3 | 0 | 3.7 | `circle_s20_f7_OFF`、`circle_s20_f10_OFF`、`circle_s20_f13_OFF` |
| A4 | 0 | 1 | 1 | 1.3 | kcheck・hold |
| A5 | 0 | 10 | 0 | 12.2 | `nephroid_xy_y3_f7_OFF`、`nephroid_xy_y8_f7_OFF`、`nephroid_xy_y5_f5_OFF`、`astroid_yz_y3_f7_OFF` ほか 6 本 |
| A6 | 0 | 3 | 3 | 4.0 | kcheck・hold |
| A7 | 0 | 6 | 0 | 7.3 | `heart_s7_f10_OFF`、`cardioid_a5p4_f10_OFF`、`circle_s14_f10_OFF`、`heart_s7_f10_OFF` ほか 2 本 |
| A8 | 0 | 1 | 1 | 1.3 | kcheck・hold |
| A9 | 0 | 9 | 0 | 11.0 | `ellipse_28x14_f8_OFF`、`ellipse_28x14_f16_OFF`、`saddle_s12_f10_OFF`、`saddle_s22_f10_OFF` ほか 5 本 |
| A10 | 0 | 3 | 3 | 4.0 | kcheck・hold |
| A11 | 0 | 6 | 0 | 8.9 | `wscan_XZ_R24yp2_a`、`wscan_XZ_R24ym2_a`、`wscan_XZ_R24yp4_a`、`wscan_XZ_R24ym4_a` ほか 2 本 |
| A12 | 0 | 1 | 1 | 1.3 | kcheck・hold |
| D1 | 0 | 3 | 0 | 3.7 | `heart_s7_f5_OFF`、`heart_s7_f7_OFF`、`heart_s7_f13_OFF` |
| D2 | 0 | 2 | 0 | 2.4 | `heart_s4_f10_OFF`、`heart_s10_f10_OFF` |
| D3 | 0 | 6 | 0 | 7.3 | `heart_s7_xp10_f10_OFF`、`heart_s7_xm10_f10_OFF`、`heart_s7_zp10_f10_OFF`、`heart_s7_zm10_f10_OFF` ほか 2 本 |
| D4 | 0 | 2 | 0 | 2.4 | `heart_rz36p_f10_OFF`、`heart_rz36m_f10_OFF` |
| D5 | 0 | 2 | 0 | 2.4 | `fish_w14_f10_OFF`、`fish_w20_f7_OFF` |
| D6 | 0 | 1 | 1 | 1.3 | kcheck・hold |
| D7 | 0 | 10 | 0 | 12.2 | `random_rms2_5-40Hz_p0_OFF`、`random_rms2_5-40Hz_p1_OFF`、`random_rms3_5-40Hz_p0_OFF`、`random_rms3_5-40Hz_p1_OFF` ほか 6 本 |
| D8 | 0 | 3 | 0 | 3.7 | `helix_xy_r5_h8_f7_OFF`、`helix_xy_r10_h4_f5_OFF`、`helix_xz_r8_y5_f7_OFF` |
| K1 | 0 | 11 | 1 | 13.5 | `tri_w14_f6_OFF`、`star5_w14_f5_OFF`、`diamond_w14_f9_OFF`、`spade_w14_f6_OFF` ほか 6 本 |
| K2 | 0 | 1 | 1 | 1.3 | kcheck・hold |
| K3 | 0 | 21 | 1 | 25.7 | `tri_w14_r180_f6_OFF`、`star5_w20_f5_OFF`、`star5_w14_t30_f5_OFF`、`diamond_w14_r45_f9_OFF` ほか 16 本 |
| K4 | 0 | 4 | 4 | 5.2 | kcheck・hold |

## S2: 146 本、180 分（暖機を足して 3.7 時間 = 3 時間 40 分）

| 段 | 粒子 | 本数 | うち kcheck・hold | 時間 [分] | 中身 |
|---|---|---|---|---|---|
| R0 | 0 | 4 | 4 | 5.2 | kcheck・hold |
| R1 | 0 | 21 | 1 | 25.7 | `heart_s5_f10_OFF`、`cardioid_a5p4_f13_OFF`、`cardioid_a10_f10_OFF`、`cardioid_a17_f10_OFF` ほか 16 本 |
| R2 | 0 | 3 | 3 | 4.0 | kcheck・hold |
| R3 | 0 | 35 | 3 | 42.9 | `eight_v_s7_f10_OFF`、`eight_v_s7_f7_OFF`、`eight_v_s7_tilt30_f7_OFF`、`infinity_s7_f10_OFF` ほか 28 本 |
| R4 | 0 | 3 | 3 | 4.0 | kcheck・hold |
| G1 | 0 | 17 | 1 | 20.8 | `star5_w40_f2_OFF`、`tri_w40_f2_OFF`、`diamond_w36_f3_OFF`、`star5_w14_xp12_f5_OFF` ほか 12 本 |
| G2 | 0 | 11 | 1 | 13.5 | `heart_s7_f2_OFF`、`circle_s14_f2_OFF`、`circle_s14_f14_OFF`、`circle_s17_f12_OFF` ほか 6 本 |
| G3 | 0 | 8 | 1 | 9.8 | `cchirp_x_r14_a0.1_OFF`、`cchirp_z_r14_a0.02_OFF`、`random_rms0.5_5-60Hz_p0_OFF`、`random_rms0.5_5-60Hz_p1_OFF` ほか 3 本 |
| G4 | 0 | 23 | 3 | 28.3 | `cchirp_x_r6_a0.1_OFF`、`cchirp_z_r6_a0.02_OFF`、`cchirp_x_r20_a0.1_OFF`、`cchirp_z_r20_a0.02_OFF` ほか 16 本 |
| G5 | 0 | 17 | 2 | 20.9 | `heart_s14_f7_OFF`、`eight_v_s14_f5_OFF`、`infinity_s14_f5_OFF`、`spade_w36_f4_OFF` ほか 11 本 |
| G6 | 0 | 4 | 4 | 5.2 | kcheck・hold |

## S3: 144 本、199 分（暖機を足して 4.0 時間 = 3 時間 59 分）

| 段 | 粒子 | 本数 | うち kcheck・hold | 時間 [分] | 中身 |
|---|---|---|---|---|---|
| B0 | 0 | 4 | 4 | 5.2 | kcheck・hold |
| B1 | 0 | 9 | 0 | 12.1 | `chirp_x_a0.05_40-110Hz`、`chirp_x_a0.1_40-110Hz`、`chirp_x_a0.2_40-110Hz`、`chirp_y_a0.05_40-110Hz` ほか 5 本 |
| B2 | 0 | 21 | 5 | 26.1 | `rotline_r26_f3_OFF`、`rotline_r26_f6_OFF`、`rotline_r26_f10_OFF`、`rotline_r23_d12_f11_OFF` ほか 12 本 |
| B3 | 0 | 12 | 0 | 20.2 | `wscan_XZ_R28_a`、`wscan_XZ_R28_b`、`wscan_XZ_R28slow_a`、`wscan_XZ_R28slow_b` ほか 8 本 |
| B4 | 0 | 3 | 3 | 4.0 | kcheck・hold |
| B5 | 0 | 26 | 0 | 37.4 | `wscan_XZ_R24yp8_a`、`wscan_XZ_R24yp8_b`、`wscan_XZ_R24ym8_a`、`wscan_XZ_R24ym8_b` ほか 22 本 |
| B6 | 0 | 3 | 3 | 4.0 | kcheck・hold |
| B7 | 0 | 19 | 1 | 27.4 | `kgrid_x3_OFF`、`kgrid_x4_OFF`、`kgrid_z1_OFF`、`kgrid_z2_OFF` ほか 14 本 |
| B8 | 0 | 4 | 4 | 5.2 | kcheck・hold |
| C1 | 1 | 43 | 9 | 57.1 | `chirp_x_a0.05_40-110Hz`、`heart_s7_f10_OFF`、`heart_s7_f7_OFF`、`cardioid_a5p4_f10_OFF` ほか 30 本 |

## S4: 86 本、114 分（暖機を足して 2.6 時間 = 2 時間 34 分）

| 段 | 粒子 | 本数 | うち kcheck・hold | 時間 [分] | 中身 |
|---|---|---|---|---|---|
| C2 | 2 | 43 | 9 | 57.1 | `chirp_x_a0.05_40-110Hz`、`heart_s7_f10_OFF`、`heart_s7_f7_OFF`、`cardioid_a5p4_f10_OFF` ほか 30 本 |
| C3 | 3 | 43 | 9 | 57.1 | `chirp_x_a0.05_40-110Hz`、`heart_s7_f10_OFF`、`heart_s7_f7_OFF`、`cardioid_a5p4_f10_OFF` ほか 30 本 |

## 学習・検証・試験の内訳（kcheck・hold は別に数える）

| | 学習 | 検証 | 試験 | kcheck・hold | 合計 |
|---|---|---|---|---|---|
| S1 粒子 0 | 68 | 7 | 19 | 21 | 115 |
| S2 粒子 0 | 99 | 14 | 7 | 26 | 146 |
| S3 粒子 0 | 81 | 0 | 0 | 20 | 101 |
| S3 粒子 1 | 27 | 2 | 5 | 9 | 43 |
| S4 粒子 2 | 27 | 2 | 5 | 9 | 43 |
| S4 粒子 3 | 27 | 2 | 5 | 9 | 43 |
| **合計** | **329** | 27 | 41 | 94 | 491 |

試験の内訳: (a) 学習に無い形 18 本（T 群 9 ＋ 楕円の速さ違い f4・f14 ＋ 系統ごと外した club 4・liss34 3）、(b) 学習した形の新しい条件 8 本（乱数の新しい種 2、ずらし 3、逆回り 1、λ/2 のすぐ上 1、大きさ 1）、(c) 替えた粒子 15 本（粒子 1〜3 × 5）。

学習の内訳: ff 244 本、走査 55 本、chirp・multisine 12 本、格子の段 18 本。違う設計の数（学習・検証・試験、kcheck を除く）: 292 種類。

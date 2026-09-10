# 拡張3D軌道データセット計測

## 概要

`Extended_Random_3D`は、低・中・高周波帯を別々に構成できる再現可能な
XYZ軌道です。既存の`Long_Random_3D`は変更せず、JSON auto計測では
ランダム軌道と既存パラメトリック形状を混在できます。

サンプル計画は`extended_3d_dataset_plan.json`です。

- `role=train`: モデル同定用のランダム励振
- `role=validation`: 未知形状への一般化評価用
- `role=diagnostic`: 診断専用

`role`は成果物のautomation metadataへ保存されます。検証形状を学習へ混ぜると
zero-shot評価ではなくなるため、学習時に分離してください。

## ハードウェアなし検証

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\extended_3d_dataset_plan.json `
  --dry-run
```

全条件の3D図と統計CSVを作成します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\extended_3d_dataset_plan.json `
  --preview-only `
  --preview-output-dir .\extended_3d_dataset_plan_preview
```

確認対象:

- `trajectory_overview.png`
- `trajectory_preview_summary.csv`
- 条件別PNG
- `amplitude_scale_applied`（1未満なら安全上限により全軸一括縮小）

## 低リスクwide midband条件

既存の5--50 Hz midband条件は`max_accel_mm_s2=40000`により一部が
約±1.0 mmへ自動縮小されます。元条件を比較基準として残したまま、同じseed・周波数成分で
実効約±1.5 mmまで広げる4条件を計画末尾へ追加しています。

| JSON index | label | 有効軸 | 実生成範囲 | 最大速度 | 最大加速度 |
|---:|---|---|---|---:|---:|
| 15 | `midband_xy_wide1p5_seed3301` | XY | X −1.50～+1.42、Y −1.50～+1.47 mm | 290.2 mm/s | 57,838 mm/s² |
| 16 | `midband_yz_wide1p5_seed3401` | YZ | Y −1.50～+1.34、Z −1.50～+1.31 mm | 243.8 mm/s | 61,351 mm/s² |
| 17 | `midband_x_only_wide1p5_seed3501` | X | X −1.50～+1.50 mm | 222.1 mm/s | 59,679 mm/s² |
| 18 | `midband_z_only_wide1p5_seed3701` | Z | Z −1.50～+1.48 mm | 177.0 mm/s | 45,286 mm/s² |

4条件とも`amplitude_scale_applied=1.0`、`risk_level=standard`、
`max_accel_mm_s2=65000`です。Y単軸の元条件はすでに−1.50～+1.36 mmへ達しているため、
同内容のwide条件は追加していません。周波数上限50 Hzは変更していません。

最初にXY wideだけを確認する場合:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\extended_3d_dataset_plan.json `
  --label midband_xy_wide1p5_seed3301 `
  --output-dir .\stereo_acoustools_3d_records_extended `
  --confirm-each-run `
  --acknowledge-extended-trajectory-safety
```

## 最初の1条件を計測

プレビュー、粒子保持範囲、装置クリアランスを確認後に実行します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\extended_3d_dataset_plan.json `
  --output-dir .\stereo_acoustools_3d_records_extended `
  --limit 1 `
  --confirm-each-run `
  --acknowledge-extended-trajectory-safety
```

全条件では`--limit 1`を外します。カメラserial、left-master同期、左LED ROI、
ステレオ校正の既定値は従来と同じです。

学習用だけを計測する場合は`--role train`、検証形状だけを計測する場合は
`--role validation`を追加します。特定条件には`--label LABEL`を使用できます。

## 後処理

計測と重い後処理は分離されています。別PowerShellで新規runを監視する場合:

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_auto_monitor.py `
  .\stereo_acoustools_3d_records_extended `
  --new-only
```

計測後にまとめて処理する場合:

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_auto_monitor.py `
  .\stereo_acoustools_3d_records_extended `
  --once
```

## Extended_Random_3Dパラメータ

主要パラメータ:

- `seed`, `duration_sec`, `sample_hz`
- `x_limit_mm`, `y_limit_mm`, `z_limit_mm`（0でその軸を固定）
- `low_f_min_hz`, `low_f_max_hz`, `low_components`, `low_weight`
- `mid_f_min_hz`, `mid_f_max_hz`, `mid_components`, `mid_weight`
- `high_f_min_hz`, `high_f_max_hz`, `high_components`, `high_weight`
- `chirps_per_enabled_band`
- `waypoints`, `drift_weight`
- `spectral_decay_power`
- `max_speed_mm_s`, `max_accel_mm_s2`

成分数またはweightを0にすると、その帯域を無効化します。各軸は指定範囲へ
正規化され、その後に速度・加速度を満たす共通倍率でXYZ全体が縮小されます。

## JSONで利用できる形状

- `Long_Random_3D`
- `Extended_Random_3D`
- `Chirped_3D`（詳細は`CHIRPED_3D_DATASET_JP.md`）
- `Cusped_3D`（詳細は`CUSPED_3D_DATASET_JP.md`）
- `Z_Axis_Vibration`, `X_Axis_Vibration`
- `Elliptical_Orbit`, `Diagonal_Line`, `Heart`, `Rectangular_Orbit`
- `Vertical_Figure_Eight`, `Horizontal_Infinity`, `Single_Slanted_Line`
- `S_Shaped_Orbit`
- `Figure_Eight_3D`, `Toroidal_Helix`, `Trefoil_Knot`, `Lissajous_3D`

パラメトリック形状は`steps_per_cycle`, `frequency_hz`, `loops`と形状振幅に加え、
正の`max_speed_mm_s`, `max_accel_mm_s2`が必須です。上限を超える場合は形状を
維持したままXYZ全体を縮小し、実倍率をmanifestと統計へ保存します。

## 対話版

- `acoustools_random3d_no_eventcam.py`: mode 6
- `acoustools_stereo_eventcam_3d_recording.py`: mode 16

3軸チャープは同スクリプトのmode 17です。
カスプ軌道は同スクリプトのmode 18です。

どちらも`Extended multi-band random 3D excitation`として帯域とXYZ範囲を入力できます。

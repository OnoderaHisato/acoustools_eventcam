# 3Dチャープ軌道データセット

## 目的

`Chirped_3D`は、XYZ各軸に独立した開始・終了周波数、振幅、位相を設定する
開軌道です。単軸チャープだけでなく、次の3D励振を同じ生成器で表現します。

- X/Zを同じスイープ、位相差90度にした掃引ヘリックス
- 軸ごとに上昇・下降方向を変えたcounter-sweep
- 線形／対数チャープ
- 振幅変調によって軌道体積が呼吸するチャープ
- XYZの周波数範囲をずらした広帯域3D励振

既存のハート軌道は10 Hzの基本波だけでなく20、30、40 Hz付近の高調波も含みます。
チャープ条件は、このような複数周波数を含む未知形状に対して、周波数応答と軸間結合を
同定するための追加データです。

## 計測計画

通常計画は`chirped_3d_dataset_plan.json`です。

- `train / standard`: X、Y、Z単軸2--50 Hz、掃引ヘリックス
- `train / challenge`: counter-sweep、対数スイープ、振幅変調付き3Dチャープ
- `validation / challenge`: 学習に含めない対数スイープと下降スイープ

保持限界診断は`chirped_3d_retention_boundary_plan.json`へ分離しています。
4条件を順番に強くしており、すべて`role=diagnostic`、
`risk_level=retention_boundary`です。

| 段階 | 範囲 | 各軸振幅 | 実最大速度 | 実最大加速度 |
|---|---:|---:|---:|---:|
| boundary01 | 10--100 Hz | ±1.5 mm | 約1,340 mm/s | 約0.71×10^6 mm/s² |
| boundary02 | 10--130 Hz | ±2.0 mm | 約2,390 mm/s | 約1.66×10^6 mm/s² |
| boundary03 | 15--150 Hz | ±2.5 mm | 約2,854 mm/s | 約2.32×10^6 mm/s² |
| boundary04 | 15--180 Hz | ±3.0 mm | 約4,406 mm/s | 約4.65×10^6 mm/s² |

boundary04だけは開始・終了テーパを外した意図的に厳しい条件です。粒子脱落の
可能性が高い保持限界診断であり、通常の同定データ収集には使用しません。

## ハードウェアなし確認

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\chirped_3d_dataset_plan.json `
  --dry-run
```

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\chirped_3d_dataset_plan.json `
  --preview-only `
  --preview-output-dir .\chirped_3d_dataset_plan_preview
```

保持限界版も、実機前に必ず同様に`--dry-run`と`--preview-only`を実行します。
生成済みプレビューの`trajectory_preview_summary.csv`では、
`amplitude_scale_applied=1.0`を確認済みです。

## 通常計画の実機計測

最初はX単軸1条件だけを計測します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\chirped_3d_dataset_plan.json `
  --output-dir .\stereo_acoustools_3d_records_chirped `
  --limit 1 `
  --confirm-each-run `
  --acknowledge-extended-trajectory-safety
```

保持、左右カメラ内の視認性、LED同期を確認後、学習用と検証用を分けて計測します。

```powershell
# 学習用
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\chirped_3d_dataset_plan.json `
  --role train `
  --output-dir .\stereo_acoustools_3d_records_chirped `
  --confirm-each-run `
  --acknowledge-extended-trajectory-safety

# 検証用
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\chirped_3d_dataset_plan.json `
  --role validation `
  --output-dir .\stereo_acoustools_3d_records_chirped `
  --confirm-each-run `
  --acknowledge-extended-trajectory-safety
```

最初のX条件を本番データとして採用した場合、学習用の再計測では
`--start-index 1`を追加して重複を避けられます。

## 保持限界診断

4条件を一括実行しません。`N`を0、1、2、3の順に1つずつ変更し、各条件後に
粒子保持とカメラ内の位置を確認します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\chirped_3d_retention_boundary_plan.json `
  --start-index N `
  --limit 1 `
  --output-dir .\stereo_acoustools_3d_records_chirped_boundary `
  --confirm-each-run `
  --acknowledge-extended-trajectory-safety `
  --acknowledge-retention-boundary-risk
```

`--acknowledge-retention-boundary-risk`がない場合、PATを開く前に停止します。
粒子が脱落した段階で終了し、それより強い条件へは進みません。
単独計画では、保持限界条件を1条件だけ選択し、`--confirm-each-run`で確認しながら進めます。
統合計画の末尾として複数条件を連続実行する場合に限り、
`--allow-retention-boundary-tail`を指定できます。この場合も各条件でpreviewとEnter確認を強制し、
`--keep-going`は使用できません。

## 後処理

別PowerShellで新規runだけを監視します。

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_auto_monitor.py `
  .\stereo_acoustools_3d_records_chirped `
  --new-only
```

計測後にまとめて処理する場合は`--new-only`の代わりに`--once`を使用します。

## JSONパラメータ

- `x_amp_mm`, `y_amp_mm`, `z_amp_mm`: 各軸の片振幅。0で軸を固定
- `x/y/z_f_start_hz`, `x/y/z_f_end_hz`: 各軸の開始・終了周波数
- `x/y/z_phase_deg`: 軸間位相
- `sweep_law`: `linear`または`logarithmic`
- `envelope`: `constant`, `ramp_up`, `ramp_down`, `sine`, `tukey`
- `taper_fraction`: `tukey`の片側テーパ比率（0--0.5）
- `amplitude_modulation_hz`, `amplitude_modulation_depth`: 振幅変調
- `max_speed_mm_s`, `max_accel_mm_s2`: ベクトル速度・加速度上限

上限を超えた場合はXYZを同じ倍率で縮小し、その倍率を
`amplitude_scale_applied`としてpreview CSVとrun metadataへ保存します。

対話版では`acoustools_stereo_eventcam_3d_recording.py`のmode 17、
カメラなし版では`acoustools_random3d_no_eventcam.py`のmode 7から利用できます。

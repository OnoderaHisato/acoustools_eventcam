# カスプを含む3D軌道データセット

## 目的

`Cusped_3D`は、ハート軌道の尖った部分と同様のカスプ（尖点）を、形状・向き・
尖点数・周波数を変えて評価するための閉軌道です。位置は連続しており、粒子を
不連続に飛ばす軌道ではありません。厳しい点は、尖点で指令速度がほぼ0になり、
直後に接線方向が大きく変化することです。

このため、次を分けて評価できます。

- 方向反転付近の追従遅れ
- カスプ形状が持つ高調波への応答
- X/Y/Z軸間の結合
- 尖点を丸めたときに誤差が改善するか

3D持ち上げでは、尖点の瞬間に第3軸速度も0になる余弦成分を使用するため、
平面曲線だけを尖らせて第3軸でカスプを消すことはありません。

## 実装形状

| curve | 尖点数 | 平面形状の主な幾何高調波 |
|---|---:|---:|
| `cardioid` | 1 | 2次 |
| `nephroid` | 2 | 3次 |
| `deltoid` | 3 | 2次 |
| `astroid` | 4 | 3次 |
| `hypocycloid` | 3--12で指定 | おおむね`cusp_count-1`次 |

第3軸持ち上げを使う場合は、尖点数に対応する高調波も加わります。preview CSVの
`highest_geometric_harmonic`と`highest_geometric_frequency_hz`に、実際の計画で
最も高い幾何周波数を記録します。

## 通常計画

`cusped_3d_dataset_plan.json`には12条件あります。

- train 9条件
  - cardioidのsharp／rounding=0.08対照
  - cardioidの3D持ち上げ
  - nephroid、deltoid、astroid
  - 5尖点hypocycloid
  - 最高幾何周波数40 Hzとなる10 Hz astroid
- validation 3条件
  - 未学習の平面、回転角、8 Hz nephroid/cardioid
  - 6尖点hypocycloid

`rounding=0`は数学的カスプです。`rounding=0.08`は同じ振幅・周波数を保ちながら
尖点だけを少し丸めた対照です。生成結果ではcardioid sharpの尖点速度／最大速度比が
約`1e-5`、rounded対照が約`0.053`で、尖点の有無を数値でも区別できます。

## 保持限界診断

`cusped_3d_retention_boundary_plan.json`は通常計画から分離しています。

| 段階 | 形状 | 基本周波数 | 最高幾何周波数 | 実最大速度 | 実最大加速度 |
|---|---|---:|---:|---:|---:|
| 01 | cardioid | 8 Hz | 16 Hz | 約310 mm/s | 約0.022×10^6 mm/s² |
| 02 | nephroid | 10 Hz | 30 Hz | 約533 mm/s | 約0.060×10^6 mm/s² |
| 03 | astroid | 20 Hz | 80 Hz | 約942 mm/s | 約0.424×10^6 mm/s² |
| 04 | 10尖点hypocycloid | 25 Hz | 250 Hz | 約4,015 mm/s | 約6.23×10^6 mm/s² |

04は粒子脱落が起こり得る意図的な保持限界条件です。通常の学習データとして一括実行せず、
01から1条件ずつ進めます。

## ハードウェアなし確認

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\cusped_3d_dataset_plan.json `
  --dry-run
```

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\cusped_3d_dataset_plan.json `
  --preview-only `
  --preview-output-dir .\cusped_3d_dataset_plan_preview
```

## 最初のsharp／rounded対照2条件

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\cusped_3d_dataset_plan.json `
  --output-dir .\stereo_acoustools_3d_records_cusped `
  --limit 2 `
  --confirm-each-run `
  --acknowledge-extended-trajectory-safety
```

この2条件で粒子保持、左右視認性、LED同期を確認後、残りのtrain条件は
`--role train --start-index 2`、validationは`--role validation`で計測できます。

## 保持限界条件

`N`を0、1、2、3の順に変え、1条件ずつ実行します。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\cusped_3d_retention_boundary_plan.json `
  --start-index N `
  --limit 1 `
  --output-dir .\stereo_acoustools_3d_records_cusped_boundary `
  --confirm-each-run `
  --acknowledge-extended-trajectory-safety `
  --acknowledge-retention-boundary-risk
```

粒子が脱落した段階で終了し、次の段階へは進みません。
単独計画では、保持限界条件を1条件だけ選択し、`--confirm-each-run`で確認しながら進めます。
統合計画の末尾として複数条件を連続実行する場合に限り、
`--allow-retention-boundary-tail`を指定できます。この場合も各条件でpreviewとEnter確認を強制し、
`--keep-going`は使用できません。

## 後処理

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_auto_monitor.py `
  .\stereo_acoustools_3d_records_cusped `
  --new-only
```

計測後にまとめて処理する場合は`--new-only`を`--once`へ変更します。

## JSONパラメータ

- `curve`: 形状名
- `plane`: 主平面`xy`, `xz`, `yz`
- `cusp_count`: hypocycloidの尖点数
- `u_amp_mm`, `v_amp_mm`: 主平面の振幅
- `out_of_plane_amp_mm`: 第3軸振幅。0で平面軌道
- `out_of_plane_harmonic_multiple`: 第3軸の尖点数対応高調波の倍率
- `rounding`: 0でsharp、最大0.5
- `phase_deg`: 開始位相
- `plane_rotation_deg`: 主平面内の回転
- `steps_per_cycle`, `frequency_hz`, `loops`
- `max_speed_mm_s`, `max_accel_mm_s2`

対話ステレオ版ではmode 18、カメラなし版ではmode 8です。

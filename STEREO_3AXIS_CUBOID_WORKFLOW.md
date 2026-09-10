# 3D直方体サイズ精度の追加測定

## 目的

この追加測定は固定の合否誤差を設定せず、カメラからの距離と物体サイズに対して、
どの程度の寸法誤差・頂点位置誤差が生じるかを測定する。

各直方体はステージで1個の発光点を3層の3×3格子へ順番に移動して再現する。
中心はpass基準点として別に撮影するため、各半幅では表面26点を撮影する。

- 面中心: 6点
- 辺中点: 12点
- 頂点: 8点

8頂点のcamera→stage変換後座標から全28組の頂点間距離を求め、
Ossila readback間距離と比較する。

- 辺: 12本
- 面対角: 12本
- 体対角: 4本

## 測定するサイズ

XYZ各軸に同じ半幅を割り当てる。

| 半幅 | 辺長 | 面対角 | 体対角 |
|---:|---:|---:|---:|
| ±20 mm | 40 mm | 56.57 mm | 69.28 mm |
| ±25 mm | 50 mm | 70.71 mm | 86.60 mm |
| ±30 mm | 60 mm | 84.85 mm | 103.92 mm |

直方体中心の論理Yは52、79、106、133 mmである。半幅30 mmの頂点と2 mmの
controlled approachを含めても、側方移動時の論理Yが安全境界20 mmを下回らず、
ステージY上限165 mmを超えないように配置している。撮影頂点の論理Y範囲は
22～163 mmである。

各D0設定の計画は1985 captureで、その内訳は次のとおり。

- orientation registration: 8
- X/Z軸controlとそのcenter: 36
- depth controlとそのcenter: 21
- cuboid sweepとそのcenter: 1920
- cuboid surface pointそのもの: 1872
- 上記surface pointのうち寸法評価に使うvertex: 576

各cuboid surface pointは4奥行中心 × 3半幅 × 26点 × 3 cycle ×
positive/negativeで構成する。26点は各軸の相対座標を`−A, 0, +A`とした
3×3×3格子から中心1点だけを除いたものである。

## 設定ファイル

- `stereo_3axis_cuboid_config_d210.json`
- `stereo_3axis_cuboid_config_d410.json`
- `stereo_3axis_cuboid_config_d610.json`

設定は、それぞれ既存の`full_001`、`far_001`、`far_002`の固定済み
`session_config.json`を継承する。dry planには展開後の完全な設定が保存される。

X/Zの新しい範囲は次のとおり。

| Axis | 論理範囲 | hardware範囲 |
|---|---:|---:|
| X | −30～+30 mm | 70～130 mm |
| Z | −30～+30 mm | 120～180 mm |

3設定とも`full_motion_envelope_and_cables_confirmed=false`にしてある。実行前に、
この拡張範囲全体について治具、ケーブル、カメラ視野、target mountを目視確認し、
該当値を`true`へ変更してから新しいdry planを作ること。soft limitだけを先に広げて
実機を動かしてはならない。

D0=210/410/610 mmへカメラを再配置する場合は、その都度D0、ステージ原点readback、
カメラ間固定、focus不変を確認する。再測定した場合は継承元ではなく、使用する
cuboid configの`absolute_distance_reference`へ新しい測定日時・readback・不確かさを
上書きして記録する。

## dry plan

例としてD0=610 mmを示す。

```powershell
.\venv\Scripts\python.exe .\stereo_3axis_stage_accuracy_capture.py `
  --config .\stereo_3axis_cuboid_config_d610.json `
  --session-dir .\stereo_3axis_stage_accuracy_records\cuboid_d610_001
```

```powershell
.\venv\Scripts\python.exe .\stereo_3axis_stage_accuracy_plot_plan.py `
  .\stereo_3axis_stage_accuracy_records\cuboid_d610_001 `
  --coordinates both `
  --show
```

D0=210/410 mmでは、configとsession名の`610`をそれぞれ`210`、`410`へ置き換える。

## 本計測

確認済みのdry planと同じconfig・sessionを指定する。

```powershell
.\venv\Scripts\python.exe .\stereo_3axis_stage_accuracy_capture.py `
  --config .\stereo_3axis_cuboid_config_d610.json `
  --session-dir .\stereo_3axis_stage_accuracy_records\cuboid_d610_001 `
  --execute
```

## 別シェルでの随時解析

次のwrapperは既存のwatch解析を実行し、capture完了後にcuboid寸法解析まで行う。

```powershell
.\venv\Scripts\python.exe .\stereo_3axis_cuboid_analyze.py `
  .\stereo_3axis_stage_accuracy_records\cuboid_d610_001 `
  --watch
```

base解析がすでに完了している場合は再処理せず、cuboid解析だけを実行できる。

```powershell
.\venv\Scripts\python.exe .\stereo_3axis_cuboid_analyze.py `
  .\stereo_3axis_stage_accuracy_records\cuboid_d610_001 `
  --skip-base-analysis
```

## 3距離の統合解析

```powershell
.\venv\Scripts\python.exe .\stereo_3axis_cuboid_analyze.py `
  .\stereo_3axis_stage_accuracy_records\cuboid_d210_001 `
  .\stereo_3axis_stage_accuracy_records\cuboid_d410_001 `
  .\stereo_3axis_stage_accuracy_records\cuboid_d610_001 `
  --skip-base-analysis `
  --output-dir .\stereo_3axis_stage_accuracy_records\combined_cuboid_analysis
```

主要出力:

- `cuboid_dimension_error_summary.png`: 辺・面対角・体対角の寸法誤差
- `cuboid_point_position_error_summary.png`: 表面26点全体の3D位置誤差
- `cuboid_point_class_error_summary.png`: 面中心・辺中点・頂点別の3D位置誤差
- `cuboid_dimension_summary.csv`: 奥行・サイズ・距離種類別統計
- `cuboid_pairwise_measurements.csv`: 全頂点対の個別結果
- `cuboid_surface_points.csv`: 表面26点すべての個別結果
- `cuboid_coverage.csv`: 表面26点と8頂点が揃ったpassの確認
- `cuboid_accuracy_report.md`: Markdown表

寸法誤差は`camera pair distance - Ossila readback pair distance`であり、正値は
ステレオ計測が長め、負値は短めに計測したことを表す。合否閾値は解析に固定せず、
用途に応じてCSVから後から選択する。

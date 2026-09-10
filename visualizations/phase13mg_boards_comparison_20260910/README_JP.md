# 13mg位相・旧A位相・上下交換版の比較

## 結論

- 現在の `acoustools_send_phase_npy.py` のラジアン入力対応は、今回のファイルに対して正しく動作しています。
  実数位相は `exp(1j * phi)` で全素子単位振幅の `complex64 (1,512,1)` に変換されます。
  13mg位相の最大循環位相誤差は **8.7453e-8 rad**、Top/Bottomとも256素子すべて非ゼロです。
- `A_previous_10mm_18V.npy` は最初から `complex64 (1,512,1)` で、全複素値を変更せず読み込みます。
  振幅はほぼ1です。ファイル名の18Vは送信スクリプトが電圧を設定する意味ではありません。
- `phase_13mg_5mmradius_w001_boards_swapped.npy` は、元の前半256／後半256を交換したものと
  実数値でも変換後の複素値でも完全一致します。各256素子内の並びは変えていません。
- AcousToolsのOpenMPD変換512インデックスは全て一意で、Topは元0–255、Bottomは256–511の範囲内です。
  3ファイルとも変換→逆変換で完全一致しました。ただし、これはソフトウェア上の対応の検証であり、
  外部最適化器がどちらの基板を先に並べたか、実機配線がどうなっているかの実測確認ではありません。
- 送信・可視化の対象テスト12件が成功しました。今回、実機送信・カメラ接続はしていません。

## 可視化

- `pressure_comparison.png`: 3ファイル × XY/XZ/YZの音圧振幅比較。共通カラースケール、水色円は直径10 mm。
- `element_phase_comparison.png`: Top/Bottomの素子位相比較。横x・縦yです。
- 各NPY名のサブフォルダ: 指定された `acoustools_visualize_phase_field.py` が生成した
  `phase_and_acoustic_field.png`、`acoustic_field_slices.npz`、`simulation_metadata.json`。
- `comparison.json`: 入力のSHA-256・形式・誤差・モデルによる力評価。

可視化は全ファイル共通で241×241点、各軸±15 mm、現在のAcousTools既定音速346 m/sです。
音圧比較図は全9断面をまとめた99.5パーセンタイルで上限を設定しています。
これらは球の散乱を含めない入射音場であり、球の輪郭は大きさの目安です。

上下交換すると、XY(z=0)の複素音場は同一、XZ/YZはz→−zの鏡像になります。
複素音場の相対L2誤差はそれぞれ2.47e-7、2.74e-7、2.63e-7で、この対応を確認しました。
したがって、XY図だけでは上下順を判定できません。

実行したコマンド（同じ出力先で再実行すると上書きするため、再確認時は新しい出力先に変更）:

```powershell
.\venv\Scripts\python.exe -B acoustools_visualize_phase_field.py phase_13mg_5mmradius_w001.npy --output-dir visualizations\phase13mg_boards_comparison_20260910\phase_13mg_5mmradius_w001 --resolution 241 --lateral-mm 15 --axial-mm 15
.\venv\Scripts\python.exe -B acoustools_visualize_phase_field.py A_previous_10mm_18V.npy --output-dir visualizations\phase13mg_boards_comparison_20260910\A_previous_10mm_18V --resolution 241 --lateral-mm 15 --axial-mm 15
.\venv\Scripts\python.exe -B acoustools_visualize_phase_field.py phase_13mg_5mmradius_w001_boards_swapped.npy --output-dir visualizations\phase13mg_boards_comparison_20260910\phase_13mg_5mmradius_w001_boards_swapped --resolution 241 --lateral-mm 15 --axial-mm 15
```

## 直径10 mm・密度24.8 kg/m³での力の参考評価

球の質量は12.98525 mg、重力は127.3853 µNです。
原点での音響放射力Fzを、元パッケージのSH/Mie流体球近似で計算しました（+zが上）。

| 入力 | 音速343 m/sのFz | 音速346 m/sのFz |
|---|---:|---:|
| 13mg w001・元の順番 | +135.547 µN | −5.901 µN |
| A previous 10mm 18V | +217.846 µN | +211.418 µN |
| 13mg w001・上下交換 | −135.547 µN | +5.901 µN |

同じ音速では上下交換により原点Fzの符号が反転します。
343 m/sでは元の順番が上向きで、重力との差は約+6.4%。交換版は下向きです。
しかし現在の346 m/sでは13mg位相の原点Fzはほぼゼロとなり、結論が大きく変わります。
**これだけで「元データは上下逆」「交換すれば浮く」とは判断できません。**
13mg位相の生成時の音速、素子座標順、基板間隔、音圧換算値を確認する必要があります。
343 m/sは比較用条件であり、今回の13mgファイルの実際の生成条件を確認したわけではありません。

3×3の力勾配行列の固有値実部は全条件で負でしたが、原点での重力との釣り合いは別条件です。
原点で釣り合わないことだけで、近傍に浮揚平衡点が存在しないとは言えません。
今回は平衡点の探索やその点での安定性評価まではしていません。
346 m/sについて展開次数12→16と差分幅0.30→0.15 mmも比較しています。

力計算は `hologram_reoptimized_eps10mm_rho24p8_20260910/physics/` のコピーを使っています。
球内音速1052 m/s、空気密度1.2 kg/m³、P_ref=3.4、素子半径4.5 mmを仮定し、
指向性を球中心で評価する近似です。実測音圧・物性による校正や、実機浮揚の保証ではありません。

元の3つのNPYと送信・可視化スクリプトは変更していません。

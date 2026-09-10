# 3軸Ossilaステージによる固定ステレオカメラの3D変位精度評価

## 1. 目的と実験の境界

この実験では、左右ステレオカメラを剛体ベースへ固定し、3台のOssila Linear Stageに
載せた小型の点滅標的をX/Y/Zの3方向へ移動させます。標的には、点滅LEDを背面に置いた
ピンホール、またはLED光を導いた光ファイバー端面を使用します。

この手順ではPATおよびAcousToolsを使用しません。PAT座標、PAT指令位置、浮遊粒子の
平衡位置、PAT用camera-to-PAT変換は、計画、収録、解析のいずれにも入力しません。

主目的は、Ossilaの位置readbackで表した既知の相対変位に対して、ステレオ再構成が
どの程度正しい3次元変位長を返すかを調べることです。主指標は次式です。

\[
\Delta \hat p_C=\hat p_{C,i}-\hat p_{C,0}
\]

\[
L_{\mathrm{stereo}}=\|\Delta \hat p_C\|,\qquad
L_{\mathrm{Ossila}}=\|\Delta s\|
\]

\[
e_L=L_{\mathrm{stereo}}-L_{\mathrm{Ossila}}
\]

ここで、\(\hat p_C\) は左OpenCVカメラ座標で得た標的中心、\(\Delta s\) は基準点に
対する3軸のOssila readback差です。ユークリッド長は剛体回転で変わらないため、
変位長の主評価にはカメラ座標とステージ座標の回転登録が不要です。

ただし、

- 1軸移動で他の2成分へどれだけ漏れるか
- 組み付けた3軸がどの程度直交しているか
- camera X/Y/Zまたは機械X/Y/Zごとの符号付き誤差

を調べる場合は回転登録が必要です。この回転は主評価とは別に取得した登録用データだけ
から求め、評価データをfitへ混ぜません。また、scale、similarity、affine変換は主評価へ
絶対に適用しません。

## 2. この実験で分かること・分からないこと

### 2.1 主として分かること

- 1軸ごとの相対変位長誤差
- 3軸を組み合わせた斜め変位の長さ誤差
- 距離に対するステレオ変位誤差の変化
- 制御した正方向/負方向接近の差、繰返し再現性、ヒステリシス
- 静止中の3D散らばり、時間ドリフト
- 別登録を用いた場合の軸方向誤差、cross-axis成分、軸の非直交性
- stage commandとreadbackの差を含めた運用上の到達性

### 2.2 そのままでは分からないこと

- Ossila stage自体の絶対位置精度
- カメラ光学中心から標的までの独立な絶対距離精度
- stereo calibrationのscale誤差とOssilaのscale誤差のどちらが原因か
- 3軸組付けのたわみ、LED標的位置、ケーブル反力、ステレオ誤差の個別寄与

Ossila readbackは比較基準として有用ですが、外部測長器による独立真値ではありません。
commandとreadbackが同じ内部位置系を共有する場合、送りねじscale、非線形、バックラッシュ
などの一部はreadbackだけでは検出できません。したがって、独立測長なしの結果は
「Ossila readback基準のステレオ相対変位精度」と表現します。

## 3. 座標系と主評価

ステレオ三角測量の一次出力は左OpenCVカメラ座標です。

- X: 左カメラ画像の右
- Y: 左カメラ画像の下
- Z: 左カメラ前方
- 単位: mm

ステージのX/Y/ZはOssila設定ファイルの論理ラベルであり、OpenCVのX/Y/Zと同じとは
限りません。主評価では、座標軸を一致させず、同じ2点間の変位長だけを比較します。

Ossila motion consoleの絶対表示 \(h\) と、本スクリプトの実験用論理座標 \(g\) は
別々に保存します。

\[
h=d+\epsilon g
\]

現在の3軸設定では、

\[
h_X=100+g_X,\qquad
h_Y=20+g_Y,\qquad
h_Z=150-g_Z
\]

です。したがって、本書の`g=[X,Y,Z]`はOssila motion consoleのhardware表示では
ありません。混同を防ぐため、測定点は原則として
`論理 g=[...] → hardware h=[...]`
の順で併記します。例えば、

- 実験原点：`g=[0,0,0] → h=[100,20,150] mm`
- 登録中心：`g=[0,100,0] → h=[100,120,150] mm`
- 単軸scan基準：`g=[0,20,0] → h=[100,40,150] mm`

です。ここでhardware座標は機械的なhard limitという意味ではなく、Ossilaが表示・
readbackする絶対位置です。

基準を \(g=0\) と置くのは計算上の平行移動にすぎず、Ossilaの絶対表示を書き換える
意味ではありません。現在の配置では光源をY stageへ載せ、hardware値が増えるほど
カメラから遠ざかります。物理的な可動端5/200 mmから各15 mm離して、
最近側の運用基準を \(h_0=20\) mm、遠方側のsoft limitを \(h=185\) mmとします。
基準から遠方を正にした奥行は

\[
s_{\mathrm{depth}}=h-h_0
\]

なので、基準で0 mm、遠方端で165 mmです。カメラから基準標的までの距離を \(D_0\)
と定義するなら、

\[
D(h)=D_0+(h-20)
\]

であり、hardware 185 mmでは \(D_0+165\) mmです。論理原点を使わずhardware値を
そのまま表示しても、差分評価は同一です。物理端5～200 mmの195 mm全域を使うことと、
両端に15 mmずつ余裕を取ることは両立しません。まず20～185 mmでpilotを行い、
端部を広げる場合は衝突・ケーブル・端点再現性を別途確認して新しいconfig/sessionを使います。

各scanでは評価点群の直前と直後に基準点を再撮影します。実装済み解析では
`center_pre`をそのscanの変位基準とし、`center_post`は主RMSへ混ぜず、戻りdrift専用に
使います。将来、時刻補間した基準中心 \(\hat p_{C,0}(t_i)\) を使う場合は

\[
\Delta \hat p_{C,i}=
\hat p_{C,i}-\hat p_{C,0}(t_i)
\]

を求めると、低周波の熱ドリフトや標的中心のゆっくりした変動を診断しやすくなります。
補間を使用した場合も、前基準、後基準、両者の差を別々に保存します。

### 3.1 1軸変位

1軸だけを動かす点を主評価の中心にします。例えばX stageだけを動かした場合、

\[
L_{\mathrm{Ossila}}=|x_i-x_0|
\]

です。この評価は、3軸の機械的直交度を仮定しません。

### 3.2 複合3軸変位

3軸readbackを直交座標とみなす場合、

\[
L_{\mathrm{Ossila}}=
\sqrt{(x_i-x_0)^2+(y_i-y_0)^2+(z_i-z_0)^2}
\]

です。ただし、この式は3本の実移動軸が互いに直交し、各軸の1 mmが同じ機械長である
という仮定を含みます。組付け誤差が無視できない場合は、独立に測量した単位軸ベクトル
\(a_X,a_Y,a_Z\) を使い、

\[
\Delta p_{\mathrm{mech}}=
a_X\Delta x+a_Y\Delta y+a_Z\Delta z
\]

\[
L_{\mathrm{Ossila,surveyed}}=
\|\Delta p_{\mathrm{mech}}\|
\]

とします。評価中のステレオ点から軸ベクトルやscaleを推定し、その同じデータの真値へ
戻すことは禁止します。

## 4. 既存ステレオ校正を流用できる条件

使用する校正は、チェッカーボード正方形一辺を

```text
square_size_mm = 7.12
```

として作成した次のNPZです。

```text
stereo_checkerboard_calib_on_stage_20260728/stereo_calibration_square7p12_final.npz
```

旧 `7.1 mm` 校正や、別のsquare寸法で作成したNPZを流用してはいけません。

既存校正を流用できるのは、少なくとも次の条件がすべて成立するときです。

- 左カメラserialが `00000508`、右カメラserialが `00000509` である
- 左右の割当てを入れ替えていない
- 左右カメラ、レンズ、スペーサ、固定ベースの相対位置・相対角度を変えていない
- フォーカス、絞り、レンズ固定状態を変えていない
- 使用解像度と画像座標系が校正時と同じである
- 校正後にカメラを落下・衝突させておらず、固定ねじの緩みがない
- 測定予定体積が両カメラの共通視野内にあり、十分な視差と正の奥行を持つ
- 測定距離が校正画像で十分に拘束された範囲から大きく外れていない
- 校正NPZのhashをsessionへコピー・固定し、収録と解析で同じNPZを使う

カメラ全体を剛体のまま机上で移設しても、左右相対姿勢が保たれていればステレオ内部校正は
原理上維持されます。ただし、移設後は既知の静止標的で、左右投影、正の奥行、再投影誤差、
距離の概略値を必ずshakedown確認します。

次の場合は再校正します。

- 片側だけを動かした
- レンズ、フォーカス、絞りを変更した
- カメラベースがたわんだ、または基線を組み直した
- 新しい測定体積で系統的な奥行ずれや大きな再投影残差が出る
- NPZ内のsquare寸法、camera serial、image sizeを確認できない

チェッカーボード寸法の不確かさはステレオ全体のmetric scaleへ伝わります。7.12 mmは
単なるラベルではなく実寸入力であるため、印刷物の伸縮や測定誤差も記録します。

## 5. 点滅標的の作り方

### 5.1 推奨順位

実験開始時は、広い角度から同じ発光中心が見えやすい「拡散光＋ピンホール」を第一候補に
します。光ファイバー端面は小型化しやすい一方、開口数と端面角度によって片方のカメラに
しか見えない場合があります。

推奨候補は次のとおりです。

1. 低輝度LED、薄い拡散材、黒色金属箔のピンホールを重ねた点光源
2. 安価なTOSLINK/POFケーブルをLEDへ結合し、その端面を標的位置とした光源
3. 裸のプラスチック光ファイバーをLEDへ結合し、研磨した端面を標的位置とした光源
4. 市販の小型インジケータLEDを減光し、前面へ交換可能なピンホールマスクを付けた光源

手持ち部品から選ぶ場合の比較は次のとおりです。

| 候補 | 長所 | 主な注意 |
|---|---|---|
| 安価なTOSLINK/POFケーブル端面 | 入手しやすく、既製connector端面なら再現性を得やすい | core径が大きい場合は点像が広がる。開口数とconnector外形により両眼から同じ端面中心が見えるか確認する |
| 裸のPOF端面 | 軽く、短く切って小型治具へ固定しやすい | 切断・研磨角、傷、曲げ、接着剤で見かけの中心が変わる |
| アルミ箔・薄板への針穴 | 安価で小さな発光点を作りやすく、拡散材との組合せで広い角度へ出せる | バリ、板厚、穴の傾き、破れにより左右の見かけ中心がずれる |
| LED前面の市販mask/小穴 | 電気・機械構成が単純 | LED lensの内部反射や発光面の非対称が穴越しに見えないよう拡散材が必要 |

いずれも、標的位置以外から出る光を黒い熱収縮チューブで遮光します。熱収縮時はLED、
POF、接着剤の耐熱温度を超えないよう、光源から外した部品で先に加工するか、低温で短時間
だけ加熱します。黒チューブ端からfiberを必要最小限だけ出し、端面位置を治具datumから
測れる構造にします。

レーザーダイオードは、眼への危険、sensor損傷、speckle、表面反射による重心変動がある
ため使用しません。

### 5.2 一般物品で作るピンホール標的

一例は次の構成です。

- 赤色または緑色の低出力インジケータLED
- 乳白色ポリエチレン片、トレーシング材、薄いPTFEテープなどの拡散材
- 黒色アルミテープ、薄いアルミ箔、遮光板
- 細い縫い針または精密ドリルで作った穴
- 熱収縮チューブ、黒色樹脂ケース、3D printした小型ホルダー
- stage上面へ固定するL字金具または剛性のある小型治具

最初は直径 `0.2～0.5 mm`程度の交換可能な穴から試します。穴を小さくしすぎると光量が
不足し、加工バリや傾斜で左右から見える発光形状が変わります。穴の前後位置、板厚、
加工バリを顕微鏡または拡大撮影で確認し、穴の物理中心を標的datumとします。

金属箔の切断面と針は鋭利です。加工時は保護眼鏡を使用し、完成後はバリと針先を露出
させず、導電箔が回路へ接触しないよう絶縁します。

LEDを穴の直後へ置くのではなく、LEDと穴の間に拡散材と短い空間を設けると、左右の
視線角から同じ穴が見えやすくなります。ケース内部の反射と隙間光は黒色材で遮光します。

### 5.3 光ファイバー端面標的

一般的なプラスチック光ファイバーの候補は、安価なTOSLINK/POFケーブル、または外径
`0.25～1.0 mm`程度の裸POFです。既製TOSLINK connector端面をそのまま使う場合も、
connector外面ではなく実際に発光しているcore端面の中心を測定点とします。切断する場合は
端面をfiber軸に直角に切り、細かい研磨紙または専用フィルムで平坦に仕上げます。端面から
後方の数cmを剛性ホルダーへ固定します。

注意点は次のとおりです。

- fiberの開口数が左右両カメラの視線角を覆うことをpreviewで確認する
- 端面の傾斜、傷、接着剤の盛り上がりを避ける
- glass fiberの破片は危険なため、一般実験ではplastic fiberを優先する
- emitter側とfiber途中を黒い熱収縮チューブで遮光し、途中からの漏れ光をROIへ入れない
- fiberの曲げとケーブル反力で端面位置が動かないよう、移動体上でstrain reliefする

左右の見え方が大きく違う場合は、fiber電流を上げる前に、端面角、開口数、カメラとの
向きを見直します。左右から端面の別々の縁を見ていないこと、両眼が同じ物理的なcore中心を
観測していることを、拡大した左右previewと小角度の標的回転試験で確認します。片側だけが
飽和する状態は許容しません。

### 5.4 安全なLED駆動

LEDを電源へ直接接続してはいけません。必ず直列抵抗または定電流回路を使用します。
例えば5 V電源、順方向電圧約2 Vの赤色LEDを約3 mAで点灯する概算なら、

\[
R \geq \frac{5-2}{0.003}\approx 1000\ \Omega
\]

なので、最初は `1 kΩ`以上から試します。実際にはLED datasheetの順方向電圧、最大電流、
抵抗の定格を確認し、必要最小限の明るさへ下げます。

点滅は、電池駆動の小型発振回路、microcontroller、またはfunction generatorと、電流定格に
余裕のあるtransistor/MOSFETによるlow-side switchで行います。最初の条件は
`500 Hz～1 kHz`、duty `30～50%`程度を目安とします。この周波数なら短いcaptureにも多数の
ON/OFF edgeを入れられますが、event帯域、tracking window、光量によって最適値は変わるため、
pilotで `500 Hz`から開始して確認します。

function generatorを使う場合も、出力へLEDを無条件に直結しません。generatorの50 Ω設定、
出力振幅、offset、最大電流を確認し、原則としてlogic信号でswitch段を駆動し、LED側には
独立した電流制限抵抗または定電流回路を置きます。microcontroller pinからLED電流を直接
取る場合もpin定格を超えないよう、switch段を優先します。

- cameraのSYNC端子からLED電流を直接流さない
- camera SYNCとLED回路を接続する場合は電圧、極性、共通GND、絶縁条件を確認する
- 未確認の外部電源、商用電源直結、裸配線を移動stage上で使わない
- 可能なら電池とdriverも最上段の移動体へ固定し、外部ケーブル反力を減らす
- 外部ケーブルが必要なら十分なservice loopとstrain reliefを設ける
- 抵抗、配線、LEDの発熱を停止状態で確認してからstageを動かす

左右カメラは従来どおりhardware syncします。LED点滅をcamera時刻へ同期しなくても、
静止中に両カメラが同じ複数edgeを観測できれば、点中心の評価は可能です。

## 6. 飽和と左右中心ずれへの対策

明るすぎる点光源は、イベント数が多いほど良いとは限りません。飽和、bloom、レンズ内反射、
閾値の非対称により、左右で異なる位置が「中心」として追跡されると、視差biasが直接
奥行biasになります。

previewとpilotでは次を確認します。

- 左右とも点がROI内にあり、他の点滅物体がない
- 発光領域が数pixel程度で、広い円盤や飽和した尾を作っていない
- 左右の点像が同程度の大きさだが、同じpixel座標である必要はない
- ON edgeとOFF edgeで求めた各カメラ中心が大きく変わらない
- positive/negative polarityを別々に処理した3D中心が許容範囲内で一致する
- LED電流を半分程度へ変えても3D中心が系統的に移動しない
- ROIを少し拡大・縮小しても中心が安定する
- 背景反射やケーブル上の漏れ光を追跡していない

飽和またはブルーミングが疑われる場合は、順にLED電流を下げ、dutyを下げ、拡散材または
neutral-density材を追加します。周波数を上げて1 edgeあたりのイベント塊が重なる場合は、
まず `500 Hz`へ戻し、tracking windowと点滅周期の関係を見直します。左右の中心差が光量や
周波数で変わる条件はfull試験へ進めません。

ピンホール板に厚みがあると、左右の斜め視線から見える開口中心が異なる場合があります。
薄い板を使い、穴軸を両カメラの中間方向へ向けます。必要なら標的を小角度ずつ回し、
左右の推定3D点が最も安定する向きを治具へ固定します。この向き調整データは評価データ
とは分けて保存します。

## 7. 3軸stageの組付け

想定構成は、固定ベース上へ3台のstageを直交するよう順に積み、最上段へ点滅標的を
載せる構成です。カメラはstageへ載せません。

現在のOssila設定例は次です。

```text
X E662608797769C2B +1
Y E662608797387B2E +1
Z E66260879740662A -1
```

X/Y/Zは論理名です。USB serial、製品コード、内部serial、実際の移動方向を現物ラベルと
read-only probeで再確認し、設定例を無条件に信用しません。

組付けでは次を確認します。

- 下段stageが上段2台、治具、標的、driver、配線を含む全質量に耐える
- 各stageの許容荷重、モーメント、速度、加速度を超えない
- overhangを小さくし、標的中心を最上段stageの支持範囲へ近づける
- ダイヤルゲージ、精密直角定規などで軸の平行度・直角度を確認する
- 全計画点だけでなく、点間を移動する全経路で衝突しない
- stage本体、上段stage、治具、LED、配線がカメラや机へ接触しない
- service loopが全可動域で張らず、光ファイバー端面や治具を引かない
- 電源OFF時にも自重で軸が動く構成でない
- emergency stopまたは各stageの電源遮断へ手が届く

3軸同時補間は初回試験で使いません。まず1軸ずつ停止・settleを確認して移動します。
複合点へ行く場合も、安全な軸順序をconfigへ固定し、毎回同じ順序を使います。

## 8. hardware座標、soft limit、原点復帰

各stageについて、Ossila hardware絶対座標、global座標、`direction`を明示的に分けます。

\[
h_j=d_j+\epsilon_j g_j
\]

- \(h_j\): Ossila hardware readback
- \(d_j\): global原点に対応するhardware datum
- \(\epsilon_j\): `+1`または`-1`
- \(g_j\): 実験で使うglobal座標

soft limitは3軸ごとに設定します。stage単体が機械的に移動可能でも、積層後の衝突、
ケーブル長、共通視野によって実験上の範囲は狭くなります。最初は両端から十分離した
pilot範囲を使い、端点を通常測定点へ自動追加しません。

原点復帰は大きな移動を伴います。組付け後にHOME方向と全経路を目視確認するまで自動homeを
許可しません。homeする場合は1軸ずつ行い、その軸上の全積載物と他軸位置を確認します。
実装上の順序は指定文字列の並びには依存せず、次に固定しています。

1. Yをhomeする指定なら、最初にYだけをhomeし、直ちに最遠側の安全位置
   `hY=185 mm`（`gY=+165 mm`）まで戻す
2. YをhomeせずXまたはZだけをhomeする場合も、先にYを同じ最遠位置へ退避する
3. 最遠Y位置のままX、次にZを1軸ずつhomeし、それぞれ承認済み基準位置へ戻す
4. 最後に通常のclearance経路を使って実験基準位置へ移動する

したがって、HOME switchまでの各経路だけでなく、HOME後の最遠Y位置への回復経路と
実験基準位置への復帰経路も事前に確認します。

異常時は3軸すべてへstopを試み、予期しない基準位置への自動復帰は行いません。通常完了時
だけ、承認済みの軸順序で安全なpark位置へ戻します。

## 9. 軸方向と符号の確認

full試験前に、各軸を安全な中央付近へ置き、1軸だけを小さく動かして符号を確認します。
最初の変位は `1～2 mm`程度、低速、低加速度とし、移動方向に十分な余裕を残します。

各軸 \(j\) について次を保存します。

- command前後のhardware readback
- global readback差
- stereoで測定した \(\Delta\hat p_C\)
- 左右画像上の点中心移動
- physical labelで見た実移動方向

`direction`を変える判断は、画面上で右へ動いたかどうかだけで行いません。Ossila
hardware値の増減、治具の物理方向、global符号の3つを対応付けます。Z stageの設定例は
`-1`なので特に注意します。

符号確認では、正方向移動後に元のreadbackへ戻り、往復差と標的の再現位置も確認します。
想定と異なる場合はfull計画を作り直し、既存sessionのconfigだけを書き換えて再開しません。

## 10. 回転登録を使う副評価

主評価の変位長には登録を使いません。軸別成分を報告したい場合だけ、独立した
`registration` datasetを先に取得します。

登録点は、中央基準から各軸の正負方向へ移動した非共面的な点を含めます。例えば、

```text
origin
+X, -X
+Y, -Y
+Z, -Z
複数の3軸組合せ点
```

を用い、すべての点を安全範囲内に置きます。Ossila readback差をsource、ステレオ変位を
targetとして、scaleなしの直交Procrustes/Kabschで

\[
\Delta p_C \simeq R_{S\rightarrow C}\Delta s
\]

の回転 \(R_{S\rightarrow C}\in SO(3)\) だけを求めます。

禁止事項は次のとおりです。

- similarity scaleを主結果へ掛ける
- 軸ごとにscaleをfitして誤差を消す
- affine/shearをfitして主結果へ適用する
- 各距離または各stationで回転をfitし直す
- evaluation点をregistration点の不足補充に使う
- registration残差をevaluation精度として報告する

scale診断値を計算する場合は、「適用していない診断値」と明記します。登録fitのRMS、
最大残差、回転行列、determinant、直交性誤差を保存し、別のhold-out点で登録を検証します。

軸の実際の非直交性やstage積層のたわみは、純回転では吸収されず副評価の残差へ残るべき
量です。

## 11. 独立な機械基準距離

### 11.1 二つのbaselineを混同しない

ステレオ校正の光学baseline \(B\) は、

\[
B=\|T_{\mathrm{stereo}}\|
\]

で得る左右光学中心間距離です。カメラ筐体間やレンズ外周間をノギスで測った値で
置き換えません。

この実験で別途記録する「機械基準距離」 \(D_{\mathrm{mech},0}\) は、基準stage位置で
標的発光中心から、固定camera rig上の再現可能な機械datumまで測った距離です。
機械datumの例は、cameraベースへ固定した精密な基準面、基準球、基準穴です。

\(D_0\)を測った瞬間のcommand値だけでなく、3軸すべての実global/hardware readback、
home状態、時刻を保存します。Y基準の実readbackを \(g_0\)、各captureのreadbackを
\(g_i\) とすると、既定configの式は

\[
D_i=D_0+(g_i-g_0)
\]

です。Yの`direction=+1`では \(g=h-20\) なので、hardware表示なら
\(D_i=D_0+h_i-h_0\) です。したがって、D0測定時のhardware readbackが
20.03 mmなら20.00 mmへ丸めません。

機械datumから光学中心または光学中心baseline直線までのoffsetが独立に分からない場合、
\(D_{\mathrm{mech},0}\) を「カメラ光学中心からの距離」と呼びません。

カメラの光学中心は筐体内部にあり、ノギス、ハイトゲージ、接触probeを直接当てることは
できません。そのため、実際に接触測定できるcamera baseの基準面・基準穴・基準球を
機械datumとして先に定義し、「どの面のどの方向から標的端面までを測ったか」を図と写真へ
残します。光学中心距離へ換算するのは、その機械datumから光学中心へのoffsetがCAD、
メーカー情報、または独立測量で不確かさ付きで得られた場合だけです。

### 11.2 光学baseline直線までの距離を定義する場合

独立測量で左光学中心 \(C_L\)、右光学中心 \(C_R\)、標的中心 \(P_0\) を同じ機械座標系に
置ける場合、質問で用いている距離は、

\[
b=\frac{C_R-C_L}{\|C_R-C_L\|}
\]

\[
D_0=
\left\|
(P_0-C_L)-b\,b^T(P_0-C_L)
\right\|
\]

です。これは標的中心から、左右光学中心を通る無限直線までの最短距離です。

光学中心の筐体内offsetが不明な場合は、ステレオ自身で推定した標的位置を独立真値へ
使いません。次のいずれかを行います。

- camera mountの基準面と光学中心のoffsetをCAD・設計値・独立測量で確立する
- CMM、測定アーム、外部trackerなどでcamera datumと標的datumを同一座標系へ測る
- optical baseline距離ではなく、明確に定義した機械基準面からの距離として報告する

このbaseline垂線距離を解析内でも絶対距離比較へ使う場合は、単に`distance_mm`を入れる
だけでは有効になりません。定義名を明示し、Y stage軸がbaseline垂線方向と一致することを
独立測量で確認して、例えば次も設定します。

```json
"definition": "target_point_to_stereo_optical_center_baseline_line_perpendicular",
"axis_alignment_verified": true,
"axis_alignment_standard_uncertainty_rad": "actual_standard_uncertainty_rad"
```

最後の引用符付き値は説明用であり、実測した数値へ置き換えます。この条件を満たした場合だけ、
解析は横方向変位がゼロのdepth sampleについて、機械距離ラベルとステレオ推定baseline
垂線距離を比較します。既定の`mechanical_reference_plane...`定義では、絶対距離ラベルは
付けても光学baseline距離との同一量比較は行いません。

一般的なノギス、ハイトゲージ、直角定規、ゲージブロックを使う場合も、測っている端点を
図と写真で残します。LED樹脂外面ではなく、ピンホール面の穴中心またはfiber端面中心を
標的datumとします。

### 11.3 不確かさ

単純な1次元距離なら、標準不確かさの例は

\[
u(D_0)=
\sqrt{
u_{\mathrm{instrument}}^2+
u_{\mathrm{repeat}}^2+
u_{\mathrm{target}}^2+
u_{\mathrm{datum}}^2+
u_{\mathrm{alignment}}^2+
u_{\mathrm{temperature}}^2
}
\]

です。

最低限、次を記録します。

- 測定器名、serial、校正日、分解能、仕様精度
- 同じ設置での繰返し測定値
- ピンホールまたはfiber中心を決める不確かさ
- camera機械datumから光学中心へのoffset不確かさ
- 測定軸とstage軸のcosine誤差
- 治具のたわみ、接触力、温度
- 基準stage readbackとその再現性

独立とみなせない共通biasを繰返し回数の平方根で減らしません。必要なら各入力座標の
共分散からMonte Carloで \(D_0\) を伝播し、標準不確かさ \(u\) と、採用したcoverage
factorによる拡張不確かさ \(U=k u\) を併記します。

絶対距離ラベルを有効にするときは、configを例えば次のように実測値で埋めます。数値は
例示せず、測定記録から転記します。

```json
"absolute_distance_reference": {
  "available": true,
  "definition": "mechanical_reference_plane_to_target_axial_distance_along_stage_y_not_optical_center_distance",
  "stage_axis": "y",
  "sign": 1,
  "reference_command_global_mm": [0.0, 0.0, 0.0],
  "reference_readback_global_xyz_mm": ["actual_x", "actual_y", "actual_z"],
  "reference_readback_hardware_xyz_mm": ["actual_hx", "actual_hy", "actual_hz"],
  "distance_mm": "measured_D0",
  "standard_uncertainty_mm": "u_D0",
  "stage_standard_uncertainty_mm": "u_stage_propagation",
  "method": "measurement procedure",
  "instrument": "instrument and serial",
  "measured_at": "ISO-8601 timestamp",
  "home_state": "homed axes and procedure"
}
```

上の引用符付きplaceholderは説明用で、そのまま実行できません。JSONには実際の数値を
入れます。`available=true`なのに実readback、不確かさ、方法、測定器、時刻、home状態の
いずれかが欠けるとスクリプトは停止します。

変位長だけを主評価する場合、\(D_0\) は合否計算に不要です。距離別に結果を示すための
横軸として用いる場合も、mechanical distance、stereo-estimated distance、Ossila
readbackから外挿したdistanceを別列に保存します。

## 12. 収録計画

### 12.1 capture単位

各点では次の順に実行します。

1. 3軸が停止していることをstatusで複数回確認
2. settle待ち
3. hardware同期した左右event capture
4. recorderで左右event数と同期時刻metadataを確認
5. 撮影後に3軸readbackとcapture中driftを確認
6. command、readback、status、時刻をmanifestへ保存

点が両眼ROI内にあるか、点追跡sample数・scatterが十分かは、収録後の解析で判定します。
温度は現スクリプトでは自動取得しないため、温度依存性を評価する場合は外部温度計の
時刻付きログを別途保存します。

移動中のイベントは主評価へ使いません。

### 12.2 基準点

実装済み既定configでは、Xはhardware 100 mm、Zはhardware 150 mm、Yは最近点側の
hardware 20 mmを実験原点`g=[0,0,0] → h=[100,20,150] mm`とします。
Yはhardware 185 mmまでの165 mm範囲を
最近点から遠方へ評価します。
最近点ではX/Zを基準位置から動かしません。視野、衝突、ケーブルを確認したうえで、
X/Zの±5/±10/±15 mm掃引はclearance位置`gY=+20 mm`（`hY=40 mm`）またはそれより遠方でだけ
行います。
長い試験では、

```text
reference_pre → evaluation point → reference_post
```

を基本単位にするか、各scanの開始・中間・終了で基準を再撮影します。基準復帰の
readback差とステレオ中心差を、driftと機械再現性の診断へ使います。

### 12.3 実装済み測定順序（8頂点方式との違い）

現在の既定planは、各Y stationで
`中心 → (±X, ±Y, ±Z)の8頂点 → 中心`
を繰り返す立方体scanではありません。実際の順序は次です。

1. camera→stage回転登録を1回行う
   - 登録中心`g=[0,100,0] → h=[100,120,150] mm`
   - X、Y、Zそれぞれの±5 mmの6点
   - 同じ中心へ復帰
2. 各repeatで奥行き別のX/Z単軸scanを正順・逆順に行う
   - Y=`20,60,100,140,160 mm`の5面
   - 各面のローカル基準`g=[0,Y,0]`
   - 各面でX±5/±10/±15 mm、Z±5/±10/±15 mmの12点
   - 各passは`local_center_pre → 12点 → local_center_post`
3. Y中心線のdepth scanを正順・逆順に行う
   - 基準`g=[0,0,0] → h=[100,20,150] mm`
   - 論理Y=`20,40,60,80,100,120,140,160,165 mm`
   - 各passの前後に基準を撮る
4. ローカルXZ対角点scanを正順・逆順に行う
   - Y=`40,100,160 mm`の3面
   - 各面のローカル基準`g=[0,Y,0]`
   - 各面でX/Z±15 mmの4隅をすべて測る
   - 各passの前後に同じY面のローカル基準を撮る
5. 2～4を3 repeat行う

したがってfullは、登録8、奥行き別X/Z軸420、depth 63、diagonal 108の
合計599 captureです。撮影座標は84点、controlled approachを含む移動座標は245点です。
この構成は、単軸scale、cross-axis、距離依存性、正逆接近差を分けて診断するためのものです。
diagonalは奥行き3面で4隅を均衡配置するため、奥行きとXZ符号の効果を混同しません。
X/Z soft limitは論理±20 mmで、±15 mm targetへの2 mm controlled approachも
論理±17 mmに収まります。

### 12.4 pilot試験

`--pilot`はorientation 8 captureに加え、X/Z ±5 mm、論理Y 20/100/165 mm、
Y=+100 mmでの対角2点を正方向/負方向から各1回測る合計41 captureです。Y=+165 mmは
soft limit端のため、実現可能な正方向接近だけです。合否限界を
決める前に実験系の破綻を検出します。

- 各軸の安全中央点
- 各軸について手動またはmotion consoleで小変位 `±1～2 mm` の符号確認
- 各軸について `±5 mm`または承認した小範囲
- 各評価点を正方向/負方向から各1回（端点は実現可能側だけ）
- 基準点の複数反復
- 少数の2軸・3軸組合せ点
- 必要なら別pilot sessionでLED電流2条件またはneutral-density 2条件
- 同じraw dataに対するpositive/negative polarityの別解析

pilotで確認する条件は次です。

- 全経路がsoft limit・衝突条件を満たす
- 全点が左右共通視野に入る
- event数と有効3D sample数が十分
- 飽和、反射、左右中心ずれがない
- 基準復帰の3D点とreadbackが安定
- 軸方向と符号が記録どおり
- capture時間、settle時間、全試験時間が現実的

pilot結果を見てtracking閾値、ROI、LED電流、settle、測定点を固定し、その後full用の
新しいsessionを作ります。pilot sessionをconfig変更後にfullとして継続しません。

### 12.5 full試験

full試験の推奨構成は次です。

- 各軸単独の複数距離点
- 各軸で制御した正方向/負方向接近
- 3～5 cycle以上
- 各scan中の基準点反復
- 独立registration dataset
- registrationに使わないhold-out検証点
- 承認済みの2軸・3軸組合せ点
- 最初と最後に同一の光量・polarity診断

主合否は1軸変位と変位長へ置き、複合点、軸別成分、回転登録後のcross-axis量は副評価に
します。測定点、cycle数、許容値は収録前にconfigへ固定します。

## 13. 安全・再現性の実装要件

3軸用スクリプトは、少なくとも次を満たす前提です。

- dry planではstage、camera、LED driverを開かない
- `--execute`時も、review済みsessionとhashが一致しなければ移動しない
- USB serial、製品コード、内部serialを軸ごとに照合する
- 3軸すべての全計画点と移動経路をsoft limit検査する
- 相対moveではなくhardware絶対座標へ変換したgotoを使う
- speed、acceleration、decelerationを軸ごとに明示・検証する
- 1軸ずつ移動し、停止確認後に次軸へ進む
- camera health checkを最初の移動前に行う
- 例外、timeout、Ctrl+Cで3軸すべてへstop/hardstopを試みる
- 異常終了時は自動home・自動returnをしない
- 通常終了時だけ承認済み順序でparkへ戻る
- calibration NPZ、stage設定、実験config、source hashをsessionへコピーする
- 一部収録済みsessionの別process/run epochでの再開を拒否する

## 14. 実行コマンド

3軸用CLIは実装済みです。probeは通常時にhome、goto、camera commandを送りません。
ただし、接続時にstageが動いていた場合だけ安全のためstopを試みます。

### 14.1 serial port一覧

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_capture.py `
  --list-stage-ports
```

### 14.2 3台のidentity/settings probe

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_capture.py `
  --config stereo_3axis_stage_accuracy_config.json `
  --session-dir stereo_3axis_stage_accuracy_records\identity_probe_001 `
  --execute `
  --probe-stage-identities
```

3軸すべての`device`、内部`serial`、`length`、`speed`、`acceleration`、
`deceleration`、`alarms`、`posmode`、`status`、現在hardware位置を保存します。
各軸の`device_response`、`serial_response`、acceleration、decelerationをconfigの
execution-lock欄へ転記し、物理ラベルと一致するまでmotionを許可しません。

### 14.3 pilot dry plan

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_capture.py `
  --config stereo_3axis_stage_accuracy_config.json `
  --session-dir stereo_3axis_stage_accuracy_records\pilot_001 `
  --pilot
```

この段階ではplanを書くだけで、serial portもcameraも開きません。`--pilot`を外すと
既定full plan（599 capture）になります。LED周波数、duty、発光径、3軸execution lock、
絶対距離設定を変更した後は、必ず古いsessionを使わず新しいdry planを作ります。
さらに`target.driver_description`、点径、周波数確認手段と、5つの
`safety_acknowledgements`を実機確認後に埋めない限りexecuteはロックされます。

dry planに固定された測定点を3D表示するには次を実行します。

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_plot_plan.py `
  stereo_3axis_stage_accuracy_records\pilot_001 `
  --coordinates both `
  --show
```

`planned_motion_targets_3d.png`をsession直下へ保存し、論理座標 \(g\) とOssila hardware
座標 \(h\) を行ごとに並べます。各行には3D overviewに加えてX–Y、Y–Z、X–Z投影を表示します。
奥行Yの範囲に対してX/Z変位が小さいため、2D投影は読みやすさを優先した独立scaleであり、
物理的な縦横比ではありません。

実験点の意味を確認する場合は論理座標だけの図が最も読みやすいです。

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_plot_plan.py `
  stereo_3axis_stage_accuracy_records\pilot_001 `
  --coordinates logical `
  --output stereo_3axis_stage_accuracy_records\pilot_001\planned_motion_targets_logical.png `
  --show
```

実機のreadback位置やsoft limitとの関係は`--coordinates hardware`で確認します。実行のたびに
`planned_motion_targets_guide.md`も生成し、各分類の目的、capture数、unique座標を記録します。
同じ座標が別の目的で複数回撮影されるため、図上のmarker数とcapture数は必ずしも一致しません。
既定ではcapture targetだけを描き、線を実際の安全経路とはみなしません。制御接近用preposition
も確認する場合は`--include-prepositions`を追加します。
スクリプトは`planned_motion_targets.csv`のSHA-256がdry-plan manifestと一致することを
確認し、stageやcameraを開きません。

### 14.4 pilot execute

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_capture.py `
  --config stereo_3axis_stage_accuracy_config.json `
  --session-dir stereo_3axis_stage_accuracy_records\pilot_001 `
  --pilot `
  --execute
```

同一config、同一`--pilot`、同一sessionを再使用しない限り実行されません。通常は
`RUN`のtyped confirmationが必要です。homeが本当に必要な軸だけを順にhomeする場合は、
例えば`--home-axes x y z`を追加します。この場合は専用確認文が必要で、`--yes`では
省略できません。引数の記載順にかかわらず、実行順は「Yをhomeして最遠位置へ回復、
X、Z」の順です。Yを指定しない場合もX/Zのhome前にYを最遠位置へ退避します。
専用の`HOME X Y Z`確認に続いて、全実験経路に対する`RUN`確認も別に要求します。
home全経路が安全だと確認できない場合は指定しません。

#### 実行開始時のstage位置

実行開始時に3軸が実験原点`h=[100,20,150] mm`にいる必要はありません。
`--home-axes`を付けない場合は、各軸について次をすべて満たす必要があります。

- controllerの`posmode`が`absolute`である
- 現在位置readbackを物理位置として信頼できる
- 停止中で、alarmがなく、HOME/END switchが作動していない
- 現在hardware位置がX=`85～115`、Y=`20～185`、Z=`135～165 mm`のsoft limit内
- 現在位置からY clearance、実験原点、最初の測定点までの実経路に障害物がない

条件を満たせば、スクリプトが現在位置から安全経路を使って
`g=[0,0,0] → h=[100,20,150] mm`へ移動してから測定を開始します。
電源再投入、controller再接続、手動での機械移動、脱調・滑り、座標への疑いがあった場合は、
過去にHOMEした経験を根拠にせず、HOME全経路を確認したうえで該当軸を再HOMEします。
現在位置がsoft limit外の場合、通常実行は拒否し、明示した`--home-axes`によるHOME以外の
回復移動を行いません。

例えば各controllerのreadbackが0 mmで、3軸すべてをHOMEし直す場合は、全HOME経路と
HOME後のY=`185 mm`退避、X=`100 mm`、Z=`150 mm`への回復経路を実機で確認してから、
実行コマンドに次を追加します。

```powershell
  --home-axes x y z
```

この場合の実際の順序は、Y HOME → Y hardware 185 mm退避 → X HOME →
X hardware 100 mm回復 → Z HOME → Z hardware 150 mm回復 → 実験原点
`h=[100,20,150] mm`です。soft limitを0 mmまで広げて未確立のreadbackからgotoする
代替にはしません。

OssilaのHOME protocolは`<home>`送信直後に`<homing>`、完了時に非同期で`<home>`を
返します。HOME中に`status?`をpollせず、設定した`motion_timeout_sec`以内の完了frameを
待ち、その後に`posmode=absolute`、hardware readback≈0、停止、END switch非作動、alarm
なしを検証します。statusのHOME flagはその瞬間にlimit switchが押されているかを表し、
firmware 1.0.1では完了後にfalseとなる実測例があるため、診断記録には残しますがHOME成立の
必須条件にはしません。途中で通信・完了検証に失敗した場合は全軸をstopしてcapture前に
中断します。

通常gotoとcapture前後の位置許容差は`readback_tolerance_mm=0.1 mm`および
`capture_drift_tolerance_mm=0.1 mm`（100 µm）、HOMEのゼロ位置許容差は
`home_readback_tolerance_mm=0.5 mm`（500 µm）です。Ossila readbackが指定値から
1 µm（0.001 mm）程度ずれることは既に許容範囲内です。これはreadbackを指定値へ丸める
意味ではなく、実際のreadbackと残差をmanifestおよびterminalへ保存・表示し、解析では
その値をstage側の観測真値として使用します。

command target自体は常にsoft limit内へ厳密に制限します。一方、soft limit境界へcommand
した後の実readbackと、そこから始める次の移動については`readback_tolerance_mm`を境界の
許容幅にも適用します。例えばY target=`20.000 mm`に対するreadback=`19.999 mm`は
残差−1 µmとして記録して許容しますが、targetとして`19.999 mm`を指定することは拒否します。

Ossila Motion Control ConsoleもPythonスクリプトも同じstage COM portを開きます。
実験前確認には純正GUIを使用できますが、Pythonのprobeまたはexecute前には各stageを
Disconnectし、アプリケーションを終了します。開いたままの場合はWindows側のCOM port
占有により接続失敗する可能性があり、スクリプトはMotion Control Consoleや他のserial
programを閉じるようエラー表示します。GUIでError indicatorとStatus Barを確認した結果は
controller側の状態であり、Python側の安全条件違反はGUIに表示されない場合があります。

preflightがsoft limit外などを検出した場合は、開いたaxisをstop/closeして結果をmanifestへ
記録します。cleanupを検証できた、かつcaptureが1件もないsessionだけが修正後の再実行対象です。
`cleanup_verified=false`となった旧sessionは手動編集せず、保存して新しいdry-plan sessionを
作ります。

fullはpilot合格後に新しいsessionで行います。

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_capture.py `
  --config stereo_3axis_stage_accuracy_config.json `
  --session-dir stereo_3axis_stage_accuracy_records\full_001

.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_capture.py `
  --config stereo_3axis_stage_accuracy_config.json `
  --session-dir stereo_3axis_stage_accuracy_records\full_001 `
  --execute
```

### 14.5 analyze

full収録と個別ステレオ処理を並行させる場合は、別のPowerShellでwatch modeを起動します。
manifest上で`capture_status=captured`になったcaptureだけを処理するため、書き込み中のrawを
読みません。新規captureごとの処理結果は各`attempt_01\stereo_3d`へ保存され、収録とcleanupが
正常完了してsession statusが`captured`になった後、一度だけ全599点の集計・CSV・plotを
`analysis_3axis`へ生成します。watch側のCtrl+Cは解析だけを止め、別processの収録は止めません。

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_analyze.py `
  stereo_3axis_stage_accuracy_records\full_001 `
  --watch
```

進行状況はterminalと`analysis_3axis\incremental_processing_status.json`へ出力します。
同じsessionに複数のwatch解析を起動しないでください。収録が失敗・中断した場合は最終集計を
行わず、完了済みの個別処理を保存したまま終了します。

収録完了後に通常解析だけを行う場合は次です。

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_analyze.py `
  stereo_3axis_stage_accuracy_records\pilot_001
```

既存の3D処理結果だけを使う場合は次です。

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_analyze.py `
  stereo_3axis_stage_accuracy_records\pilot_001 `
  --skip-processing
```

正式sessionでは`--skip-processing`も既存ファイルを無条件には信用しません。capture時の
左右raw hash、校正hash、tracking条件、処理スクリプトhash、3D NPZ hashを
`stereo_3d/processing_manifest.json`と照合します。不一致またはmanifest欠落のcaptureは
usableにせず、coverage不完全として扱います。`--skip-processing`を付けない通常解析では、
欠落または古い処理結果を同じ固定条件で再処理します。599 captureのbatch処理では、
各captureの診断3D plotを省略し、集計用plotだけを`analysis_3axis`へ生成します。
`stereo_process_recording.py --skip-tracking`で既存CSVを再利用した結果は手動診断専用で、
正式な3軸解析はraw eventから生成したprovenanceではないため受理しません。

## 15. 実装済み出力

sessionには次を保存します。

- 収録時configと入力・sourceのhash
- LED/fiber種別、周波数、duty、発光径などのtarget条件
- 3軸identity、serial、設定、soft limit、status履歴
- captureごとのcommand、撮影前後readback、到着誤差、capture中drift
- 左右raw event dataと追跡・三角測量結果
- captureごとの処理provenance
  `stereo_3d/processing_manifest.json`（raw、校正、tracking、source、3D出力のhash）
- 左カメラ座標の3D時系列
- Ossila論理readback `stage_readback_[xyz]_mm`と、motion consoleに対応する
  `stage_hardware_[xyz]_mm`
- 遠方を正にした基準からの相対奥行`stage_depth_from_reference_mm`
- 左カメラ前方成分`camera_forward_z_mm`
- 左カメラ光学中心からのステレオ斜距離
  `camera_left_optical_center_slant_distance_mm`
- 左右光学中心を通るbaseline直線までのステレオ推定距離
  `stereo_baseline_distance_estimate_mm`
- robust静止中心、sample数、scatter RMS/P95/max
- `delta_camera_[xyz]_mm`
- `delta_stage_[xyz]_mm`
- camera→stage変換後の各点`measured_stage_[xyz]_mm`
- 変換後座標とstage readbackの差`absolute_stage_residual_[xyz]_mm`
- `camera_displacement_norm_mm`
- `stage_displacement_norm_mm`
- `distance_norm_error_mm`
- stage軸方向、横方向、3D残差
- 正/負接近差、cycle間再現性、center return drift
- orientation専用点から求めたscaleなし回転、rigid fit残差
- similarity/affineの「非適用」診断値
- calibration由来の数値Jacobian理論共分散・距離感度

解析先`analysis_3axis`にはsummary JSON、Markdown report、`measurements.csv`、
全captureの目的・座標・基準点・集計先をまとめた`capture_role_map.csv`、
`validation_accuracy.csv`、`axis_response.csv`、`repeatability.csv`、
`forward_reverse.csv`、`return_drift.csv`、`theory_per_capture.csv`と主要plotを
生成します。

重なりを避けたvalidation詳細出力として、axis validationは軸ごとの
`validation_axis_x.png`、`validation_axis_y.png`、`validation_axis_z.png`（計画に存在する
軸だけ）へ分け、さらに各Reference Y面を別panelにします。depthは
`validation_depth.png`、diagonalはXZ四隅を別panelにした`validation_diagonal.png`へ
分離します。反復点の横方向jitterは表示専用で、CSV・summaryの値には適用しません。
種類・軸別のcount、distance bias/RMS/P95/max、axial/lateral/3D RMS、XYZ residual RMSは
`validation_summary_by_type_axis.csv`とMarkdown版 `.md`へ出力します。

解析後、距離誤差、XYZ残差、全capture一覧、論理3D測定点を連動表示するには次を
実行します。このviewerはstageとcameraを開きません。

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_viewer.py `
  stereo_3axis_stage_accuracy_records\pilot_001
```

標準ではローカルWebサーバーとWebGL画面が起動します。3D領域は実寸比の
stage論理座標を使い、論理Zを画面の鉛直方向として表示します。床面と奥面のgridは
既定で表示します。

- 左drag: 制限のない360度回転
- mouse wheel: 拡大・縮小
- 右drag: 視点の平行移動
- `Fit points` / `Fit all`: 測定点だけ / 表示中OBJを含む全体へfit
- `Perspective`: 初期透視視点
- `Top`、`Bottom`、`Front`、`Back`、`Left`、`Right`: 各標準視点へ移動

3D点、2D plot、または全capture一覧へhoverすると、対応するq番号、計画座標、
reference、各誤差を連動表示します。clickで選択を固定し、Escで解除します。
同じ論理座標で複数回撮影したcaptureは、選択できるよう表示上だけX/Z方向へ扇状に
offsetします。点線が真の座標を示し、右欄の座標値と解析値にはoffsetを適用しません。
`重なりを分離`をoffにすると真の座標へ戻せます。

3D左下の凡例は、sphere=`Single-axis`、cube=`Depth`、diamond=`Diagonal`、
cone=`Orientation fit`、多面体=`Reference / Return drift`を表します。上部の
`Marker` sliderはこれらのmarkerと黄色い選択haloを同じ比率で変更します。既定値は
60%です。

独立測定したbaseline垂線距離を診断表示する場合は`--d0-mm`を指定します。この値は
viewer表示専用であり、正式な解析configや合否値を上書きしません。

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_viewer.py `
  stereo_3axis_stage_accuracy_records\pilot_005 `
  --d0-mm 220
```

camera基準で配置定義されたOBJは、orientation点から求めたcamera→stage剛体変換と
合成してstage座標内へ表示できます。過去のPAT基準configは新しいstage-only実験へ
流用しません。既存camera基準configを使う例は次です。

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_viewer.py `
  stereo_3axis_stage_accuracy_records\pilot_005 `
  --d0-mm 220 `
  --cad-obj drawings\openmpd_case_Twin_20260717.obj `
  --cad-config drawings\openmpd_case_Twin_20260717_camera_overlay.json
```

`CAD`で表示を切り替え、`opacity`で透明度を変更します。`Fit points`は測定点だけ、
`Fit all`は表示中のOBJを含む全体へfitします。CAD配置はステレオ由来のR,tを使い、
独立D₀へ強制的に位置合わせしません。このためD₀との差は独立診断として残ります。

2026-07-31版event-camera caseは、CAD内で左右光軸が±20°、baseline方向がCAD Z、
camera前方がCAD −Xです。外装holderのcamera前方端面はCAD X=18.0 cmです。
次の専用configは、回転をcamera/CAD対応から求めたあと、stage Y方向の平行移動だけを
独立実測「case前端面からstage開始基準`g=[0,0,0]`まで210 mm」で拘束します。

```powershell
.\venv\Scripts\python.exe stereo_3axis_stage_accuracy_viewer.py `
  stereo_3axis_stage_accuracy_records\pilot_005 `
  --d0-mm 220 `
  --cad-obj drawings\stereo_eventcam_case_20260731.obj `
  --cad-config drawings\stereo_eventcam_case_20260731_camera_stage210_overlay.json
```

この拘束後、case前端面代表点はstage `Y=-210.000 mm`、面法線とstage +Yの差は
0.901°、stage原点から面への垂直距離は209.974 mmです。viewer左下にもこの診断を
表示します。`D₀=220 mm`は光学baseline直線までの別定義なので、case前端面210 mmと
同じ値へ統合しません。

従来のMatplotlib画面が必要な場合は`--native`を指定します。自動試験用PNGは
`--no-show --output <path>`で引き続き生成できます。

## 16. 結果の解釈

### 16.1 最優先で見る値

最初に見る値は、1軸移動ごとの

```text
camera_displacement_norm_mm
stage_displacement_norm_mm
distance_norm_error_mm
```

です。これらは回転登録なしの値を正式結果とし、diagonal/multi-axis点は積層軸の
直交度を仮定するためsecondary集計へ分けます。

静的plotの横軸`Stage displacement norm from each pass reference`はglobal座標では
ありません。例えばSingle-axisの`g=[0,20,0]`を基準とする
`g=[0,15,0]`と`g=[0,25,0]`は、どちらも基準から5 mmなので横軸5 mmへ描かれます。
X/Zの±5 mm点も同じ位置へ重なるため、pilotでは5 mm付近に12 captureが集中します。
塗り色はX/Y/Z、枠色とmarker形状はSingle-axis/Depth/Diagonalを表します。
DiagonalはY=100 mmが支配的でも、枠とdiamond markerでDepthから区別します。

次に、

- 同じ長さでの距離依存性
- 正方向/負方向接近差
- cycle間のbiasとscatter
- 基準復帰誤差
- static scatter
- 有効capture率

を確認します。

平行ステレオの近似では、1カメラ当たりの点中心標準偏差を
\(\sigma_{\mathrm{px}}\) とすると、

\[
\sigma_Z \approx
\frac{Z^2}{fB}\sqrt{2}\,\sigma_{\mathrm{px}}
\]

です。現校正は平均 \(f_x=1774.483\) px、光学中心間基線
\(B=120.138\) mm、\(fB=213183.2\) px·mmです。例えば
\(\sigma_{\mathrm{px}}=0.25\) pxなら、単一点の近似
\(\sigma_Z\) はZ=200/300/400 mmで約0.066/0.149/0.265 mmです。実カメラは収束配置なので、
正式出力`theory_per_capture.csv`では各実測3D点について、歪みを含む投影、undistort、
triangulationを数値微分します。これは画素ランダム誤差だけの予測で、校正bias、同期誤差、
発光径、飽和、stage誤差を含みません。

### 16.2 回転登録後の値

camera→stage変換は解析に実装済みです。登録専用の6点だけを使い、scaleを変更しない
Kabsch/SVD剛体fitで

\[
p_S=R_{S\leftarrow C}p_C+t_{S\leftarrow C}
\]

を求めます。ここで \(p_C\) は左カメラ座標、\(p_S\) はOssilaの論理X/Y/Zへ向きを
合わせたstage座標です。validation点はfitへ使用しないため、評価点の誤差を変換で
吸収しません。変位については平行移動が消え、

\[
\Delta p_S=R_{S\leftarrow C}\Delta p_C
\]

となります。

変換行列、平行移動、登録RMSは
`analysis_3axis/stereo_3axis_stage_accuracy_summary.json`の`orientation`へ保存します。
`validation_accuracy.csv`では次の列がstage軸表示です。

- `observed_stage_[xyz]_mm`: stereo変位をstage軸へ回転した値
- `delta_stage_[xyz]_mm`: Ossila readbackによる比較変位
- `residual_stage_[xyz]_mm`: 上記2つの差

`measurements.csv`の`measured_stage_[xyz]_mm`は変換後の各点、
`absolute_stage_residual_[xyz]_mm`は変換後座標とstage readbackの差です。
求めた行列は`analysis_3axis/camera_to_stage_transform.npz`にも
`R_camera_to_stage`、`t_camera_to_stage_mm`として保存します。

一方、`delta_camera_[xyz]_mm`と`camera_[xyz]_mm`は左カメラ座標のままです。
主指標`||Δcamera||-||Δstage||`は回転しても長さが変わらないため、この登録を必要としません。

回転登録後のX/Y/Z残差は、方向別の原因を調べる副指標です。登録RMSが小さくても、
registrationに使用した点の精度を証明したことにはなりません。hold-out点で確認します。

similarity scaleやaffine fitで誤差が小さくなっても、それは校正・stage scale誤差を
補正してしまった結果です。主結果の置換には使いません。

### 16.3 距離表示

結果の横軸は、次を混同せず名前付きで保存します。

- 左カメラZのステレオ推定値
- 左光学中心からのステレオ斜距離推定値
- 光学中心baseline直線へのステレオ垂線距離推定値
- 独立に測ったmechanical datum distance
- Ossila readback差から構成した相対距離

独立な \(D_0\) と軸surveyがない場合、「カメラから実距離何mmまで絶対精度○mm」とは
結論しません。「Ossila readback基準で、列挙した相対変位・測定点において誤差○mm以内」
と報告します。

さらに、Ossila readbackは外部測長器による独立真値ではありません。
`stage_truth.independent_external_displacement_truth_available=false`のままなら、解析の
PASS表現は`agreement_with_Ossila_encoder_readback`に限定され、camera単独の絶対精度は
`not_evaluable`になります。外部encoder、レーザー干渉計、CMM、校正済みゲージ列などを
導入した場合も、現schemaのflagをtrueにするだけでは不十分です。captureごとの外部変位
vector、時刻、provenanceを取り込む実装がないため、現在はtrueを明示的に拒否します。
外部測長入力を実装して初めて、その値へ真値を切り替え、標準不確かさとguard bandを
設定します。

### 16.4 合否範囲

測定点の間を自動的に補間して保証しません。基準点から連続して合格した、実際に測定した
点の範囲を示します。途中で不合格になった後、遠い点が再度合格しても連続合格範囲を
延長しません。

full試験の閾値はpilot後、full収録前に固定します。閾値変更、tracking変更、除外規則変更は
新しいanalysis revisionとして記録し、都合のよい条件だけを最終結果へ採用しません。

## 17. 実施前チェックリスト

- [ ] PAT/AcousToolsを起動せず、設定・sessionにもPAT依存がない
- [ ] camera `00000508/00000509` と左右割当てを確認した
- [ ] `square_size_mm=7.12` の指定校正NPZとhashを確認した
- [ ] レンズ、focus、aperture、stereo固定状態を確認した
- [ ] 3台のUSB serial、製品コード、内部serial、directionを確認した
- [ ] 3軸の荷重、moment、soft limit、全経路、衝突余裕を確認した
- [ ] LEDに直列抵抗または定電流回路を使用した
- [ ] 左右とも非飽和で、ON/OFF・polarity・光量による中心ずれを確認した
- [ ] target datumをピンホール中心またはfiber端面中心として記録した
- [ ] cable/fiberへstrain reliefを施し、全可動域で張らない
- [ ] 各軸の小変位でhardware/global/物理方向の符号を確認した
- [ ] mechanical reference distanceの定義、測定端点、実readback、不確かさを記録した
- [ ] pilotのdry planをreviewした
- [ ] full用閾値と除外規則を収録前に固定した
- [ ] 主結果へrotation、similarity scale、affine補正を適用しない

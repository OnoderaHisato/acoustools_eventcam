# Project handoff

## 2026-09-10: GitHubコード同期の初期設定

- ユーザー指定の公開リポジトリ `https://github.com/OnoderaHisato/acoustools_eventcam.git` を `origin` として、このフォルダを `main` ブランチのGitリポジトリに初期化。Git Credential Managerで `OnoderaHisato` の認証を確認し、このリポジトリのHTTPS認証ユーザーに固定した。他アカウントの認証やグローバルGit設定は変更していない。
- コミット作者はローカル設定のみ `OnoderaHisato` / `165984056+OnoderaHisato@users.noreply.github.com`。認証トークンはリポジトリに保存しない。
- `.gitignore` を許可リスト方式で追加。ルートおよび選定したサブフォルダのPython・スクリプト・手順書・計画／設定JSONを対象とし、計測データ、NPY、校正・画像・動画、外部SDK、venv、最適化結果、dependencies、source_snapshot、会話ログのエクスポートを除外。比較用の `visualizations/phase13mg_boards_comparison_20260910/compare_maps.py` とREADMEは対象とした。除外ファイルはローカルに保持する。
- `README.md` に主な入口、実機安全注意、別途必要なSDK・入力データ、手動同期コマンドを追加。GitHubへのコード保存は、実験データや実行環境の完全バックアップではない。新規フォルダのコードは `.gitignore` の許可ルール追加が必要。
- PAT・カメラ・ステージは起動せず、計測成果物と既存Pythonコードは変更していない。初回コミット／pushの成否は `git status -sb` と `git ls-remote origin refs/heads/main` で確認する。
- 公開前の確認でパスワード欄を含む `archive/genx320/GENX320_RPI5_SETUP_NOTES.md` も除外した（元ファイルは変更なし）。送信・可視化のhardware-free単体テスト12件成功。

## 2026-09-10: 13mgラジアン位相・旧A・上下交換版を可視化／比較

- ユーザー指定の `phase_13mg_5mmradius_w001.npy`、`A_previous_10mm_18V.npy`、`phase_13mg_5mmradius_w001_boards_swapped.npy` を現行 `acoustools_send_phase_npy.py` でhardware-free検査。実数2本はfloat64 `(1,512)`から `exp(1j*phi)` によりcomplex64 `(1,512,1)`へ正しく変換され、最大循環位相誤差8.7453e-8 rad。旧Aは元のcomplex64をそのまま維持。全ファイルでTop/Bottom各256素子が非ゼロ。
- 上下交換版は元の256要素ブロックの交換と実数・複素とも完全一致。OpenMPDの512インデックスは一意で基板間の混入なし、3ファイル全て変換／逆変換完全一致。外部最適化器の元の座標順・実機配線まで確定したものではない。
- 指定 `acoustools_visualize_phase_field.py` を3本とも実行。±15 mm、241×241、既定c=346 m/s。成果物と共通カラースケール比較図は `visualizations/phase13mg_boards_comparison_20260910/`。XYは上下交換前後で一致し、XZ/YZはz鏡像、複素音場の相対L2差2.47–2.74e-7。図を目視確認した。
- 10 mm球・rho=24.8・cp=1052・P_ref=3.4のSH/Mieモデルで原点の力と3×3Jacobianも比較した。c=346でFzは元13mg −5.901 µN、旧A +211.418 µN、交換13mg +5.901 µN。c=343では順に+135.547、+217.846、−135.547 µN。目標mg=127.3853 µN。全条件の固有値実部は負だが、原点の重力釣り合いとは別であり、平衡点探索・実機浮揚は未検証。元13mgの生成時音速・座標順・音圧校正を確認すべきで、上下交換が実機に正しいとの断定はしない。
- 新規比較スクリプト・数値JSON・日本語READMEを同出力先に保存。元3NPYと送信／可視化スクリプトは変更なし。対象送信・可視化テスト12件成功。新しい比較依頼へ切り替えたため、先行の再最適化NPYの送信は保留。今回PAT・カメラは開いていない。

## 2026-09-10: 新規フォルダで24.8 kg/m³の再最適化NPY生成（未送信）

- `hologram_reoptimized_eps10mm_rho24p8_20260910/` に元パッケージ直下・旧NPYのsnapshotと物理コードコピーを保存。SciPy 1.15.3を同フォルダ内dependenciesにだけ導入し、元venvは変更していない。新入口 `run_reoptimization.py` は旧v3 XYZ解を初期値とし、元波長のc約343 m/sで密度24.8の再最適化を500更新実施。
- 目的関数は元v3の力一致＋Jacobian対角和。候補の3×3Jacobian固有値を検査し、復元性を満たす中で力誤差最小の反復15を選択。更新前の評価と対応する複素位相そのものを保存して1ステップ不一致を解消。NPYを読み戻して同じ力・Jacobianになることを確認した。
- `results/phase_acoustools_optimized_eps10mm_rho24p8_v3_xyz_complex64.npy` はcomplex64 `(1,512,1)`、SHA-256 `ec915d3e6550c01fd190561b181cc32641fdb6adedd4978fcf49254500444c02`。高精度再評価Fz約126.1655 µN、目標127.3853 µNとの差約−0.96%。固有値実部は負、次数10/12/16と差分0.30/0.15 mm、NumPy/PyTorch力計算の一致を確認。元ファイルとsnapshotのハッシュ照合成功。
- 再最適化とNPY生成は完了。実機送信の追加依頼があったが、送信前に外部13mg位相の診断へ切り替わったためこのNPYは未送信。浮揚実証・実際の音速／音圧校正は未検証。詳細は同フォルダREADMEと結果JSON。

## 2026-09-10: 送信入口をラジアン実数位相へ対応・渦ホログラムを実機送信

- `acoustools_send_phase_npy.py` が `complex64` に加えて実数ラジアン位相を受理するようにした。`complex64` は shape `(1, 512, 1)` のみ、実数（float32/float64）は `(1, 512)` と `(1, 512, 1)` の両方を許可する。`complex128` や整数dtypeは従来どおり拒否する。
- 実数入力は `exp(1j * φ)` で単位振幅の `complex64` へ持ち上げてから渡す。AcousTools の `Levitator.levitate` は実数テンソルに対して `amp = torch.ones_like(hologram)` を取るため、実数のまま渡す場合と実機への指令は同一で、検証と可視化の複素経路を1本に保てる。`phase_13mg_5mmradius_w001.npy` の512素子で位相の最大往復誤差は8.75e-08 rad。
- 位相は 2π の剰余で定義されるため、`[-π, π]` 外の値は拒否せず `exp(1j * φ)` で明示的に折り返し、該当素子数を `[WARN]` 行に出す方針にした。当初案の「範囲外は拒否」から変更している。最適化器の非ラップ出力を無条件に弾かないためで、折り返しが起きた場合は必ず表示される。
- `[CHECK]` 出力へ、元ファイルのshape/dtype、解釈した形式、実機へ送る形式、実数入力時の元位相範囲を追加した。実数入力時は「振幅情報を持たないため全512素子が振幅1.0で駆動される」旨を `[WARN]` で必ず表示する。複素入力の `|a| <= 1` 検証と挙動は変更していない。
- `test_acoustools_send_phase_npy.py` を4件から9件へ拡張し、実数ラジアンの読み込み、float32/3次元shape、往復精度、非ラップ入力の折り返しと計数、shape/非有限値の拒否、非対応dtypeの拒否を確認した。9件成功。可視化・渦・粒子条件の既存テスト18件も成功。
- `phase_acoustools_focus_vortex_m2_counter_rotating_complex64.npy`（SHA-256 `8421d8b8...`）を board ID `(101, 3)` へ2回実機送信した。2回目は 18:34:53--18:39:53 の正確に300秒間出力し、`turn_off()` と `disconnect()` が正常完了、終了コード0。現在PATはOFF・切断済み。カメラ、粒子移動、記録は行っていない。
- この渦ホログラムは中心が圧力ノードで、放射力・Gor'kov・安定性の評価は未実施の診断用音場である。10 mm球に対する検証済みトラップではない。

## 2026-09-10: 中心焦点＋位相特異度2の音響渦ホログラムを生成・球周りの音場を評価

- 原点の音響焦点（`kd_solver`）へ、らせん位相 `exp(1j * s * 2 * atan2(y, x))` を素子ごとに掛けるホログラム生成入口 `acoustools_vortex_charge2_hologram.py` を追加した。`s = +1` は軌道角運動量を+z方向（+z右ねじ）に向ける。既定の `--convention counter_rotating` はbottom `s=+1`、top `s=-1` で、実験室座標で上下逆巻きになる。
- 主要出力は `phase_acoustools_focus_vortex_m2_counter_rotating_complex64.npy`、SHA-256 `8421d8b8ae4c92c4a3a1ff6ed4a6ff4360092102f52f391963fb68a8de9bbef6`。「topを逆巻き」は実験室座標基準か各面の放射方向基準かで解釈が2通りあるため、両面 `s=+1` の比較用 `phase_acoustools_focus_vortex_m2_co_rotating_complex64.npy`（SHA-256 `b8963c1b9726cafbff1eb65574b805125767674b5f02360706af93ff6fbfa597`）も生成した。後者はAcousTools標準 `add_lev_sig(mode='Vortex')` を位相特異度2へ拡張したものに相当する。
- 両方ともshape `(1, 512, 1)`、dtype `complex64`、振幅 `0.99999988--1.00000012`、前半256がtop・後半256がbottomのAcousTools順を維持する。素子外周リングの位相巻き数はcounter_rotatingがtop `-2` / bottom `+2`、co_rotatingが両面 `+2`。counter_rotatingはtopがbottomの物理x鏡像と完全一致（最大位相差0）。位相特異度が偶数のため両者ともz軸まわり180度回転で不変（最大位相差0）。
- 10 mm EPS球（半径5 mm、`ka = 3.632`、λ = 8.65 mm、音速346 m/s）を原点に置いた条件で `acoustools_vortex_charge2_sphere_field.py` により `acoustools.Utilities.propagate` の音場を評価した。XY/XZ/YZ中心断面（±20 mm、401×401）、球表面181×361、z=0面のリング（1/2.5/5/10 mm）、半径・軸方向プロファイルを計算する。
- counter_rotatingの合成音場は渦ではなく4葉の定在四重極になる。z=0面ではbottomの `e^{+i2φ}` とtopの `e^{-i2φ}` が等振幅で重なり `cos(2φ)` になるためで、r=5 mmリングのm=+2とm=-2のエネルギー比は厳密に0.500/0.500、カイラリティ差は0。球表面|p|は0--2324.82 Pa、立体角平均1084.61 Pa。球表面の4葉はθとともにねじれ、上下の逆巻きが風車状に見える。
- co_rotatingは位相特異度2の真の渦になる。r=5 mmリングでE(+2)=0.975、E(-2)=0.025、位相巻き数1.99999990、|p|は1966.44--2721.64 Paのほぼ一様なリング。残るm=-2は16×16正方格子の4回対称による折り返しで、r=10 mmではE(+2)=0.997へ下がる。
- 両者とも中心は圧力ノードで、原点|p|はそれぞれ5.30e-05 Pa、6.21e-05 Paの数値ゼロ。z軸全体が渦芯のため軸上|p|は1e-4 Pa台の数値雑音であり、x=5 mmの平行線では最大1966.70 Pa（z=0）となる。
- 図・数値NPZ・metadataは `visualizations/acoustools_vortex_charge2/<npy stem>/` に保存した。`test_acoustools_vortex_charge2.py` 11件と既存の送信・可視化・粒子条件テスト11件が成功し、`acoustools_send_phase_npy.py` のhardware-free検査にも両NPYが合格した。PAT・カメラは開いていない。放射力・Gor'kov・安定性の再計算と既存成果物の変更は行っていない。

## 2026-09-10: ホログラム最適化の粒子密度を24.8 kg/m³へ修正

- ユーザー指定に従い、`hologram_optimization_package/particle_parameters.py` に共通値 `PARTICLE_DENSITY_KG_M3 = 24.8` を追加。最適化3本、安定性評価2本、`force_profile.py`、`validate_gorkov.py` の計7本が参照する。Mie散乱と重力目標の双方に反映し、Gor'kov比較も両モデルで同じ密度を使う。
- 直径10 mm・半径5 mm・g=9.81は維持。質量12.9852496 mg、重力127.3852989 µN。`force_profile.py` は古い最適化JSONの重力を流用せず再計算し、出力 `meta` を評価条件で更新、元条件は `source_optimization_meta` に分離する。固定位相の評価であることと元密度を表示する。
- 既存の最適化JSON・力プロファイル・図・`phase_acoustools_optimized_eps10mm_v3_xyz_complex64.npy` と同metadataは変更していない。対象12ファイルの前後SHA-256が全件一致。既存成果物は40 kg/m³の過去の計算であり、24.8 kg/m³の再最適化済みNPYではない。固定位相の入射音圧可視化は密度を使わない。
- READMEへ新条件・旧結果の区別、別作業ディレクトリで再計算して既存結果を保護する注意を追記。現venvはSciPy未導入で、既知の最適化保存位相と評価力の1ステップ不一致も未修正。今回の範囲は密度設定の修正であり、再最適化・放射力再計算・PAT・カメラは実行していない。
- `test_hologram_particle_parameters.py` を追加し、共有密度の7本への接続、3最適化の質量/重力計算、力評価metadataの新旧分離を確認。`python -B -m unittest test_hologram_particle_parameters -v` の4件が成功。実行スクリプトをimportせず選択した設定代入だけを評価するhardware-freeテスト。

## 2026-09-10: 最適化EPS10mm NPYをVisualiser描画・元パッケージ音場と比較

- `phase_acoustools_optimized_eps10mm_v3_xyz_complex64.npy` を既存のAcousTools標準Visualiser入口で描画し、`visualizations/acoustools_visualiser/phase_acoustools_optimized_eps10mm_v3_xyz_complex64/` に素子位相・音圧振幅・音圧位相PNGとmetadataを生成した。
- 比較用 `acoustools_compare_optimization_package.py` を追加。元JSONの `w=(1.0, 1.0, 1.0)` と全512素子complex64完全一致を確認し、`pressure_field.json` と同じXZ全体161×241・周辺151×151の計61,602点を評価した。
- 現在の音速346 m/sでは音圧振幅の相対L2誤差は全体21.6069%、球周辺7.51774%。保存波長からの波数（音速約343 m/s）だけをpropagate引数へ渡すと、全体0.0000561%、周辺0.0000585%、複素音圧相対L2も約6.43e-7まで一致した。P_ref=3.4、素子半径4.5 mmは現行値を維持し、ゲイン・全体位相のフィットをせず再現した。
- 比較図・数値NPZ・metadata JSON・日本語説明は同出力先の `package_comparison/` に保存。元パッケージと同じ横z・縦xに統一した。AcousTools定数・元NPY・元パッケージは変更していない。球の放射力・安定性の再計算ではなく入射音場の一致確認。PAT・カメラは開いていない。

## 2026-09-10: 1 cm EPS用v3最適化済みホログラムを送信用NPYに変換

- `hologram_optimization_package/optimize_results_v3_stable.json` の `w=(1.0, 1.0, 1.0)` を選び、保存済み `x_real + 1j*x_imag` を `phase_acoustools_optimized_eps10mm_v3_xyz_complex64.npy` に変換した。shape `(1,512,1)`、dtype `complex64`、振幅ほぼ1。前半256がtop、後半256がbottomのAcousTools順を維持し、追加シグネチャ・振幅正規化・順序入替は行っていない。
- 出力SHA-256 `a8e5fea9d75d537f9e3ec665e4f021aac3f4a5c94497829967f7adab01c6595d`。元JSONのSHA-256、選択条件、粒子条件は同名 `_metadata.json` に保存。全512要素の実部・虚部のfloat32往復一致と、送信スクリプトの読み込み検査に合格した。
- 最適化の再実行ではなく既存解の形式変換。元v3には更新前の力評価と更新後の保存位相が1ステップずれる既知の問題があり、物理条件・実機18 V音圧の校正も未検証。今回の変換では力の再評価、PAT出力、カメラ接続を行っていない。

## 2026-09-10: KD solverで原点の静止トラップNPYを生成・検証
- 既存パイプラインと同じ `transducers(16, BOARD_POSITIONS)` → `create_points(1,1,x=0,y=0,z=0)` → `kd_solver()` → `add_lev_sig(mode="Trap")` で `phase_acoustools_kd_center_trap_complex64.npy` を生成した。shape `(1,512,1)`、dtype `complex64`、全素子振幅ほぼ1、SHA-256 `6c7b7bed6f488f282b95c41f780117bfde46a1cb279c0695d2fe6186cc905b23`。生成条件は同名 `_metadata.json`。
- top/bottomは前半/後半256で、両面とも物理x/y反転対称誤差0。`acoustools_send_phase_npy.py` のhardware-free検査に合格した。
- AcousTools標準Visualiser出力を `visualizations/acoustools_visualiser/phase_acoustools_kd_center_trap_complex64/` に生成した。加えて原点を必ず含む241×241格子で `propagate()` を直接評価し、同フォルダの `numeric_verification/` にPNG/NPZ/JSONを保存した。原点は圧力ノード約0.000458 Pa、軸方向の隣接ピークはz約-2.333 mmで約9431.7 Pa。これは単純な高音圧焦点ではなく `Trap` シグネチャ付きの粒子保持用静止トラップ。PAT・カメラは開いていない。

## 2026-09-08: A+B phase-only左右対称18Vマップを生成・検証
- `A_plus_B_phase_only_18V.npy` を `exp(1j * angle(A + B))` で生成した。shape `(1, 512, 1)`、dtype `complex64`、振幅 `0.99999994--1.00000012`、SHA-256 `9822d190f9de25ba677990bba88f837220c4a5d8f30ec5516b5df191a01c7eef`。
- `A+B=0` のbottom側2素子（flat index 321, 433）は物理x方向の鏡像ペアであり、両方に `0 rad` を割り当てた。top/bottomとも物理x反転に対する最大複素差0。詳細は `A_plus_B_phase_only_18V_metadata.json`。
- `acoustools_send_phase_npy.py` のhardware-free検査に合格。AcousTools標準 `Visualiser.Visualise()` の素子位相・圧力振幅・圧力位相を `visualizations/acoustools_visualiser/A_plus_B_phase_only_18V/` に生成した。PAT・カメラは開いていない。

## 2026-09-08: A/B 18V 位相マップの複素和を生成・AcousTools Visualiserで検証
- `A_paper_style_18V.npy` と `B_mirror_x_18V.npy` はともに shape `(1, 512, 1)`、dtype `complex64`、全素子の振幅がほぼ1の複素位相マップであることを確認した。Bはtop/bottom各16x16についてAの物理x方向反転と数値一致する。
- 文字どおりの複素和 `A_plus_B_complex_sum_18V.npy`（`A+B`、振幅0--1.9993978）と、同じ相対複素分布を保ち振幅を1以下にした `A_plus_B_complex_average_18V.npy`（`(A+B)/2`、振幅0--0.9996989）を生成した。両方ともshape `(1, 512, 1)`、dtype `complex64`。和はtop/bottomとも物理x反転に対する最大複素差0。
- 由来・SHA-256・振幅範囲・対称性検査は `A_plus_B_18V_metadata.json` に保存した。位相が正反対で完全相殺する素子が2個あるため、両出力は振幅一定のphase-onlyマップではない。
- 1/2スケール版をAcousTools標準 `Visualiser.Visualise()` でhardware-free描画し、`visualizations/acoustools_visualiser/A_plus_B_complex_average_18V/` に素子位相、圧力振幅、圧力位相、metadataを保存した。PAT・カメラは開いていない。

## 2026-09-04: AcousTools組み込みVisualizer版を追加・Gドライブへ追補

- 従来の`acoustools_visualize_phase_field.py`はAcousTools `propagate()`で音場計算し描画を独自Matplotlibで行っていた。追加の`acoustools_builtin_visualiser_phase_field.py`は音場描画に`acoustools.Visualiser.Visualise()`を明示的に使い、`propagate_abs()`の音圧振幅と`propagate_phase()`の音圧位相をXY/XZ/YZ断面で保存する。素子位相のみVisualizer対象外のため補助Matplotlib図とした。
- regular/17Vの両NPYを160×160、半幅40 mm、Visualizer depth 2でhardware-free描画し、それぞれ素子位相PNG、音圧振幅PNG、音圧位相PNG、metadata JSONを生成した。PAT・カメラは開いていない。
- 新スクリプトと生成物9ファイルを`G:\マイドライブ\Experiment\20260904`へ追補し、READMEへ実行コマンドと旧版との差を記載した。9ファイルはコピー元とbyte数・SHA-256が全件一致。更新後はmanifest自身を除く22エントリ、全23ファイル・9,927,289 bytes、manifest再照合エラー0、pyc混入0。

## 2026-09-04: NPY位相送信・可視化一式をGドライブへ保存

- 当日作成した送信・可視化スクリプト2本、hardware-freeテスト2本、regular/17V位相NPY 2本、両者の可視化成果物各3本、簡易日本語README、SHA-256 manifestを`G:\マイドライブ\Experiment\20260904`へ整理して保存した。
- READMEへAcousTools公式GitHub `https://github.com/JoshuaMukherjee/AcousTools`、公式documentation/PyPI、現在のvenvを使った検査・実機送信・再可視化・テストコマンド、Top/Bottom対応、17V名はスクリプトによる電圧設定ではない注意を記載した。
- コピー元とコピー先12ファイルはbyte数不一致0、SHA-256不一致0。コピー先は全14ファイル・8,566,732 bytesで、`COPY_MANIFEST_SHA256.csv`の13対象（manifest自身を除く）は欠落・size・hash error 0。コピー先からregular NPYのhardware-free検査と対象テスト7件が成功した。PAT・カメラは開いていない。Gドライブのクラウド同期完了状態は別途確認が必要。

## 2026-09-04: 元の事前計算位相を実機送信・順序往復・音場再検証

- ユーザー指定の`phase_acoustools_regular_complex64.npy`（SHA-256 `7e15d8dbc944de4002207dc9d82f8920850fda5c8f290e6af70136ac482a657d`）を、board ID `(101, 3)=(Top, Bottom)`へAcousTools通常順序変換付きで実機送信した。USB/OpenMPD接続と`Phase map sent. Output is active.`を確認し、確認時間を置いた後に全素子`turn_off()`と`disconnect()`が正常完了した。現在PATはOFF。
- 512要素のOpenMPD変換をhardware-freeで逆変換し、全indexが一意、Top source範囲`0--255`、Bottom source範囲`256--511`、複素値完全一致、最大循環位相誤差`0 rad`、最大振幅誤差`0`を確認した。
- AcousTools `propagate()`によるXY/XZ/YZ音場を同じSHA-256入力から再計算した。既存PNGの同名保存がWindows側で拒否されたため既存成果物は変更せず、再検証結果を`phase_acoustools_regular_complex64_revalidation_20260904/`へ新規保存した。送信・再検証でカメラ、粒子移動、記録は行っていない。

## 2026-09-04: 17V事前計算位相を実機送信し音場を可視化

- `phase_acoustools_regular_complex64_17V.npy`を実機送信した。事前検査はshape `(1, 512, 1)`、dtype `complex64`、全要素有限、振幅`0.99999994--1.0`、SHA-256 `77f4cd761a084ae4d6af9827eba61dfc8f80cfcaa362d844b6f83c252a04be5b`。
- 現行2枚PATのboard ID `(101, 3)=(Top, Bottom)`へ、前半256素子をTop、後半256素子をBottomとしてAcousTools通常順序変換付きで送信した。OpenMPDは正常接続し、`Phase map sent`を確認した後、確認時間を置いて`turn_off()`と`disconnect()`が正常完了した。カメラ、粒子移動、記録は行っていない。
- 同じ位相をAcousTools標準2枚PATピストンモデルでhardware-free計算し、`phase_acoustools_regular_complex64_17V_visualization/`へ素子位相・XY/XZ/YZ複素音圧PNG、数値NPZ、metadata JSONを保存した。

## 2026-09-04: 事前計算済みAcousTools位相NPYの安全な単発送信入口を追加

- `acoustools_send_phase_npy.py`を追加し、`phase_acoustools_regular_complex64.npy`のようなAcousTools順の単一複素ホログラムを現行PAT board ID `(101, 3)`へ送信できるようにした。カメラ、粒子移動、軌道計算、記録は行わない。
- 実機接続前に`complex64`、shape `(1, 512, 1)`、有限値、複素振幅上限1を検査し、SHA-256・振幅範囲・位相範囲を表示する。既定はhardware-free検証だけで、実送信には`--send`と端末での`SEND`入力を必須とする。
- 送信はAcousToolsからOpenMPDへの通常の素子順変換を有効にし、複素数の偏角を位相、絶対値を振幅としてそのまま使う。送信後はEnterまで保持し、正常終了・Ctrl+C・例外のいずれでも`turn_off()`と`disconnect()`を試みる。
- 対象位相ファイルはshape `(1, 512, 1)`、`complex64`、全要素有限、振幅`0.99999994--1.0`であることをhardware-free確認した。実PAT出力はこの追加作業では行っていない。
- `acoustools_visualize_phase_field.py`を追加した。上下16×16の素子位相と、AcousTools標準2枚PATピストンモデル`acoustools.Utilities.propagate()`で計算したXY/XZ/YZ中心断面の複素音圧（振幅・位相）をPNG/NPZ/JSONへ保存する。計算はチャンク化され、実機を開かない。

## 2026-09-02: B1再収録6 runの粒子抽出・3D・理想比較とON/OFF評価を完了

- NumPy scalar静止トラップ修正後に再収録したハート4 runと代表ランダム2 runについて、左右12 NPZの粒子抽出、ステレオ3D再構成、全runの`ideal_comparison_3d/stereo_ideal_comparison.npz`生成を完了した。元NPZとRAWは保持している。
- raw tracking valid率は全カメラ・全runで100%。ステレオ3D有効点はハート各35,000/35,000、ランダムOFF 105,000/105,000、ON 104,997/105,000。
- 代表ランダム`balanced_xyz_8s_seed2101`は、元の理想軌道rに対するベクトルRMSがOFF 0.4854 mm、ON 0.3617 mmで25.5%改善。x/y/zも18.3%/28.0%/34.9%改善し、最大ベクトル誤差も1.9294 mmから1.0052 mmへ低下した。
- ハート7 mm・10 Hz・1 s・10周回は、ベクトルRMSがペア1で0.5054→0.5351 mm（5.9%悪化）、ペア2で0.5217→0.5668 mm（8.6%悪化）。この条件ではONで理想軌道へ近づいたとは判定しない。
- 比較は各runの剛体fitで軌道形状を評価する。ランダムは`t>=0.5 s`、ハートは`t>=0.15 s`。詳細は`B1_MEASUREMENT_RESULTS_20260902.md`。
- 比較図の日本語表示のため、B1ハート／ランダムoverlay解析スクリプトのフォントをWindows搭載`BIZ UDGothic`へ変更して再生成した。
- 6 runの左右イベントNPZを含む全成果物、解析コード、レポートを`D:\measurement_control_b1_20260902_results`へ保存した。コピー対象323ファイル、2,928,426,158 bytesは欠落0・サイズ不一致0・SHA-256不一致0。全ハッシュは同フォルダの`TRANSFER_INVENTORY_SHA256.csv`。

## 2026-09-02: B1 inverse2nd指令のNumPy scalar静止トラップ不具合を修正・再収録待ち

- 2026-09-01の小規模B1セッションは、ON 3本が静止トラップとなったため補償評価には無効。AcousTools `create_points`が`np.float64`座標を黙って採用せず`(0,0,0)`を残す一方、旧`inverse2nd_feedforward.py`がNumPy配列を`tuple(row)`で返していたことが原因。既存収録は削除していない。
- `apply_inverse2nd_feedforward()`は各座標を明示的に組み込み`float`へ変換して返すよう更新された。10 Hz・800点/周期・10ループのハート指令について全2400座標が`type(value) is float`、指令が非ゼロであることをhardware-free確認した。
- ハート10 Hz・1秒と代表ランダム`balanced_xyz_8s_seed2101`のdry-run、対象ファイルの`py_compile`が成功した。ハートは補償生値最大1.4844 mm、1 mm制限率57.75%、代表ランダムは最大0.9974 mm、制限率0%。PAT、OpenMPD、カメラは開いていない。
- 更新時に上書きされたサブフォルダ用モジュール探索と後処理スクリプト参照を、ランダム／ハート双方のドライバと収録コアへ再適用した。修正版でハート4収録と代表ランダム2収録をペアごと再収録する。

## 2026-09-01: B1をハート＋代表ランダムの小規模6収録へ更新

- B1実験フォルダへハート軌道専用のA/Bドライバと収録コア、周期境界対応の2次逆モデル実装、検証スクリプトが追加された。主実験はハートOFF/ON 2ペアの4収録と`balanced_xyz_8s_seed2101` OFF/ON 1ペアの2収録、合計6収録とする。
- 更新で上書きされたサブフォルダ用モジュール探索を、ランダム／ハート双方のドライバと収録コアへ再適用した。後処理コマンドはプロジェクト直下の`stereo_acoustools_3d_postprocess.py`を参照する。
- 対象実装の`py_compile`、ハート既定10 Hzと代表ランダムのhardware-free dry-runが成功した。10 Hzハートは補償生値最大1.4844 mm、1 mm制限率57.75%。5 Hzを`--require-unclipped-ff`付きで検証すると最大0.7356 mm、制限率0%。PAT、OpenMPD、カメラは開いていない。

## 2026-09-01: B1逆モデル前置補正A/Bをサブフォルダ配置のまま起動可能化

- `measurement_control_b1_20260901/` 内のB1実験ドライバと収録コアへモジュール探索設定を追加し、B1固有の`inverse2nd_feedforward.py`/`delay_feedforward.py`は同フォルダ、既存のステレオ収録・軌道モジュールはプロジェクト直下から読み込む構成にした。
- 収録完了後にmanifestへ保存する後処理コマンドは、B1フォルダ内ではなくプロジェクト直下の`stereo_acoustools_3d_postprocess.py`を参照するよう修正した。
- `B1_README.md`を現配置の実行コマンドへ更新した。実機コマンドは`measurement_control_b1_20260901/acoustools_stereo_b1_ff_ab.py`を入口とする。
- B1対象5 Pythonファイルの`py_compile`、実験ドライバ・収録コア・比較スクリプトの`--help`が成功した。全8軌道のhardware-free `--dry-run`も成功し、48収録構成、最大補償量1.0 mm以下、制限率0--12.80%を確認した。PAT、OpenMPD、カメラは開いていない。

## 2026-08-27: 単眼X軸の原点取得を有効サンプル数lockから位置安定lockへ変更

- 安全修正後のbaseline `mono_x_feedback_hardware_20260827_150510`は68 sample（約34 ms）後、相対距離0.1562 mmで停止。P-only `...150730`は最初のcontrol sampleが0.4618 mmで即時停止した。両runとも追跡はvalid、OpenMPDへの新geometry送信は0回、trapは中心保持であり、安全interlockは意図どおり動作した。制御性能比較には使用しない。
- 両RAW先頭の旧1秒lockを再解析した。baselineはX範囲0.283 mm・標準偏差0.068 mm、P runは範囲0.905 mm・標準偏差0.123 mmで、旧実装は粒子が動いていても2000 valid sampleだけでlockしていた。P runのlock末尾は1秒中央値から0.376 mm離れていたため、P補正前から大変位だった。
- `assess_lock_stability()`を追加し、直近0.25秒のX射影位置についてrobust標準偏差（1.4826 MAD）<=0.05 mm、5--95 percentile幅<=0.12 mm、末尾--中央値<=0.05 mmを評価する。全条件が0.25秒連続で成立した場合だけ直近窓のXY中央値へlockする。15秒で成立しなければ補正を開始せず中心保持でabortedにする。
- CLIへ`--lock-window-sec`、`--lock-timeout-sec`、3つの安定閾値を追加した。`lock_policy`と最終/採用`lock_diagnostics`をmanifestへ保存し、待機中は1秒ごとに`[LOCK WAIT]`を表示する。
- RAWオフライン再生で最新不安定2本の最長連続安定時間は0.0855秒／0.0430秒となり、新判定ではlockされない。以前の正常baseline `...143059`は0.8945秒でlockでき、拒否だけに偏らないことも確認した。
- 対象単体テストは14件全成功、`py_compile`も成功。PAT、OpenMPD、カメラは実装・検証中に開いていない。次回は相対距離interlock 0.30 mm、絶対trap offset 0.02 mm、slew 0.5 mm/sを維持し、まずbaselineを再取得する。

## 2026-08-27: 単眼X軸フィードバックの相対変位安全制限をインターロックへ修正

- 成功取得済みbaseline `mono_x_feedback_hardware_20260827_143059`はRMS 0.0778 mm、旧P-only `...143224`はRMS 0.2432 mmで、後者は3.12倍に悪化した。`comparison_20260827_143430`では約1.5秒後からトラップが+0.05 mmへ張り付き、粒子が+0.25--0.45 mmへ偏った。
- 原因は旧`PaperPid1D.update()`が相対距離超過時に指令を`measured +/- max_trap_particle_mm`へclipしていたこと。正の粒子変位に対する負の復元指令を正側へ反転でき、さらに絶対offset上限の後で相対clipしていたため正帰還になった。この2 runの制御性能比較は無効とするが、追跡・処理時間の診断値は利用できる。
- 相対距離制限をcommand clampからinterlockへ変更した。現在送信中の実トラップ、slew適用後の次要求、またはlookup量子化後に実送信するgeometryと粒子の距離が上限を超えた場合、新しいgeometryを送信せず、最後に送ったトラップを保持してrunをabortedにする。正常終了以外は中心へ自動復帰しない既存方針を維持する。
- `control_log.csv`へraw/filtered error、unsaturated/requested trap、現在/要求相対距離、出力飽和・相対超過・slewの個別フラグを追加した。manifestにも安全方針を記録する。
- 回帰テストを7件から11件へ増やし全件成功。旧失敗条件`measured=+0.248 mm, kp=0.05, offset=0.05 mm, relative=0.15 mm`で、指令が+0.05 mmへ反転せず直前値を保持して超過を報告すること、およびlookup量子化による境界超過も検出することを確認した。対象3ファイルの`py_compile`も成功した。
- 同じ上限で2秒のhardware-free simulationに成功。最終位置-0.0655 mm、末尾0.5秒RMS 0.0499 mm、最大trap 0.00492 mm。最終検証成果物は`mono_feedback_records/mono_x_feedback_simulate_20260827_150222`（途中検証`...145955`も保持）。PAT、OpenMPD、カメラは開いていない。
- 次の実機確認は同じ照明・ROI `540,350,640,430`・各5秒で、baselineは`kp=0, offset=0.02 mm, relative=0.15 mm, slew=0.5 mm/s`、P-onlyは`kp=0.01`かつ同じ上限から開始する。手順は`MONO_EVENTCAM_X_FEEDBACK_JP.md`を参照する。

## 2026-08-27: 初回単眼runはROI不一致でlock前中止・現ROI候補を特定

- `mono_x_feedback_hardware_20260827_140832`（baseline）と`...141543`（P-only）は、ともに`lock_failed_0_valid_of_2000_required_samples`で補正開始前に安全中止した。`control_samples=0`で`control_log.csv`はなく、比較対象にはならない。PATは中心を保持し、制御出力は適用されていない。
- 両RAWを先頭1秒だけ軽量監査し、粒子イベント位置が指定ROI`520,430,650,520`ではなく約`(589,390) px`にあることを確認した。現ROI候補は`540,350,640,430`。baseline RAWはこのROIで1998/2000 sliceが既定閾値を通る。
- P-only RAWは同じ位置の粒子イベントがbaselineより約100分の1まで弱く、既定閾値では0/2000。閾値を極端に下げると位置標準偏差が数pixelまで悪化するため採用しない。再取得時はROIに加えてレーザー/照明がbaselineと同じ強度・角度で粒子イベントを継続生成することを確認する。
- `mono_feedback_compare.py`は`control_log.csv`欠落時にPython tracebackを出さず、manifestのstatus、abort_reason、control_samplesと再取得要求を表示するよう修正した。

## 2026-08-27: 論文再現用・左単眼X軸リアルタイムフィードバック入口を追加

- Bos et al. *Precise position feedback control of acoustically levitating objects by event-based vision* の最小再現として、左カメラ`00000508`のみ、PAT X軸のみを扱う専用入口`mono_eventcam_1axis_feedback.py`を追加した。既定はハードウェアなしsimulationで、実機は`--mode hardware`、明示ack、tight ROI、対話checkpointがない限り開かない。
- `mono_feedback_core.py`へ論文形式`C(s)=kp[Hm(s)(1+tau_d s)+1/(tau_i s)]`の離散1D制御器、4点移動平均、measurement derivative、出力/相対変位/slew制限、積分上限、conditional anti-windup、2次遅延プラントsimulationを実装した。
- 現行ステレオ校正とcamera-to-PAT登録から左画像のPAT X局所射影を計算する。現校正では`7.8554 px/mm`、画像単位方向`(0.99965,-0.02657)`。イベント明点の絶対位置オフセットはrun開始時1秒中央値lockで除く。
- 実機入口はX lookup hologramを事前計算し、制御中にKD solverを呼ばない。量子化位置が変わったときだけ1 geometryを送信する。初回上限は10秒、PAT絶対offset 0.25 mm以下。既定はP-only `kp=0.05`、offset±0.20 mm、相対変位0.30 mm、slew 5 mm/s。`kp>0.10`またはI/Dには追加ackを要求する。
- 追跡20連続欠落、制御deadline 20連続超過で停止する。異常時は観測なしの中心復帰をせず最後のtrapを保持し、操作者が粒子を確保してからPATを停止する。正常時のみ中心へ戻す。
- `mono_feedback_compare.py`でbaseline/feedbackのRMS、追跡有効率、送信・処理時間を比較できる。手順と合格条件は`MONO_EVENTCAM_X_FEEDBACK_JP.md`。
- 対象単体テスト`test_mono_feedback_control.py` 7件成功。論文の12 Hz、zeta=0.014、delay 5.5 msモデルに対する論文PID simulationも成功。実機、PAT、カメラはこの追加作業では開いていない。

## 2026-08-26: Plan B全51条件完了・最終成果物追補転送

- G:側の最終フォルダー全体を`D:\20260826_2`へミラーした。G:とD:は1,294ファイルで相対パス欠落0、余分0、サイズ不一致0、重要資料のSHA-256不一致0だった。計測データは変更・削除せず、D:ミラー完了を記す引継ぎ文書だけをG:/D:双方で同じ内容へ更新した。
- B1 30条件、B2 15条件、B3 6条件の計画51条件がすべて収録済みとなった。成功runディレクトリはB1 36、B2 15、B3 7の計58本で、B1の安全確認・成功重複とB3旧40 pilotは削除せず保持している。
- 全58 runを軽量監査し、`capture_complete=true`、左右NPZ存在・manifest記録サイズ一致、左Master/右Slave同期、共通時刻区間完走、左LED ROI `600,0,1280,180`検出、PAT送信errorなしを確認した。重い粒子抽出・3D後処理は実行していない。
- B2 particle1/2/3の6 runは`20260826_P01/P02/P03`として完了。B3事前計算版6 runはactive-PAT暖機目標0/5/10/5/5/5分をすべて達成した。実測は40が0.9610分、41が5.1391分、42が10.1384分、43--45が5.1385--5.1392分。
- 未転送だったB2 particle 6 run、B3全ツリー、現行Plan B plan/exportを既存転送先`G:\マイドライブ\Experiment\20260826_2`へ追補した。B1 626、B2 262、B3 127、plan 238の計1,253ファイル・75,411,686,604 bytesについて、転送元の全相対パスとbyte数がG:側に存在し不一致0件である。
- G:側の旧B3 exportは`export_B3_drift_source_pre_active_pat_timer_20260826`へ退避し、現行exportを配置した。旧plan/context exampleも`.pre_active_pat_timer_20260826`として保存した。
- 完了版の詳細は`PLAN_B_MEASUREMENT_HANDOFF_20260826.md`、全58 runは`PLAN_B_RUN_INVENTORY_20260826.csv`、全29 sessionは`PLAN_B_SESSION_AUDIT_20260826.csv`を参照する。

## 2026-08-26: B3 active-PAT暖機タイマーと事前計算

- `plan_b_context.json`を正としてB3を`40=0分、41=5分、42=10分、43--45=5分`へ更新し、plan/exportのlabelを一致させた。旧15/30分exportは`plan_b_20260825/export_B3_drift_source_legacy_warm15_warm30_20260826/`へ削除せず退避した。
- B3静止60秒は、PATの非ゼロ出力を始める前にホログラムを計算する。完全に同一な600,000 frameを1 geometry × 600,000 loopへ圧縮し、同じ60秒・10 kHz commandを再生する。
- 非ゼロ保持ホログラムの送信完了時を暖機の起点とし、contextの`actual_warmup_minutes`まで残り時間だけ自動待機してからカメラを起動する。実測のactive-PAT→camera開始時間、目標達成、意図的な待機時間をsession、`pipeline_manifest.json`、`pat_camera_timing.json`へ保存する。
- 2026-08-26 19:24の`40_shield_off_warm00_static_60s`は旧実装でactive PAT→camera開始が約254秒だったため、warm00本計測ではなくpilot/タイミング診断として保持する。削除・上書きはしていない。
- B3全6 labelと問題になった`41_shield_off_warm05_static_60s`のhardware-free dry-runに成功。Plan B、capture、HF、auto、workflowの対象テスト83件が成功した。PAT・カメラ・recordingは実行していない。

## 2026-08-26: Plan B B1完了・B2 drive A/B/C完了・成果物移送

- `stereo_acoustools_3d_records_plan_b1/` の成功36 runを軽量検査した。計画30条件は全て揃い、50%安全確認1 runと、中断・再開に伴うfull-scale成功重複5 runを含む。重複は削除していない。
- `stereo_acoustools_3d_records_plan_b2/` はdrive A/B/C（振幅scale 1.00/0.85/0.70）のstatic、staircase-ringdown、probe multisine各1本、計9 runまで完了。B2 particle1/2/3の6 runとB3の6 runは未計測。
- 全45 runで `capture_complete=true`、`processing_status=pending`、左右NPZ存在・サイズ一致、左Master/右Slave hardware sync、共通時刻区間完走、左LED ROI `600,0,1280,180`検出、PAT送信errorなしを確認した。重い粒子抽出・3D後処理は実行していない。
- B1/B2収録、`plan_b_20260825/`、引継ぎ文書・CSVを `G:\マイドライブ\Experiment\20260826_2` へコピーした。主要3ツリー996ファイル、40,228,917,223 bytesは転送元とG:側で相対パス・byte数が全件一致した。クラウド同期完了状態は別途Google Drive側で確認が必要。
- 詳細は `PLAN_B_MEASUREMENT_HANDOFF_20260826.md`、run一覧は `PLAN_B_RUN_INVENTORY_20260826.csv`、session監査は `PLAN_B_SESSION_AUDIT_20260826.csv`、転送ファイル一覧は `PLAN_B_TRANSFER_FILE_INVENTORY_20260826.csv` を参照する。

更新: 2026-08-26

## 2026-08-26: Plan B追加計測B1/B2/B3を現行ステレオautoへ統合

- `G:\マイドライブ\Experiment\20260826\`の引き継ぎを読み、B1小振幅マルチサイン梯子30 run、B2 z約297 Hz線の駆動/粒子依存15 run、B3ドリフト環境条件6 run、合計51 runを`plan_b_20260825/`へ統合した。
- 現行`hf_identification_trajectory.py`で10 kHz exportを再生成した。Gドライブ版とのrun集合・sample rate・全`offset_mm`を比較し、全51 runが許容差1e-15 mm以内（最大差8.33e-17 mm）で数値的に一致した。3 exportすべてのhardware-free dry-runに成功した。
- B2のstaircaseをTier A--Hへ誤分類しない専用`measurement_family=plan_b2_z_line_source`として追加した。既存Tier A--Hのtier、escape-boundary、安全ackは維持している。Tier Hの1 hardware session 1 run制限は、既存テストの期待どおり再び有効化した。
- B1 exportはbaseline後、rank 0--4のrank-major順へ並べた。hardware実行はbaselineまたは1 rankだけに制限し、HOLD 7 runへ専用ackを要求する。全Plan B runでpreview/Enterを強制し、`--keep-going`は禁止する。
- B2 drive A/B/Cはtrajectory metadataから全トランスデューサ振幅を1.00/0.85/0.70へ適用する。静止保持、開始点移動、trajectory送信、中心復帰で同じscaleを使い、位相は維持する。scaleはpipeline manifest、trajectory source、automation metadataへ記録する。
- `--plan-b-context-json`を追加し、B2粒子ID/外観、B3実予熱・囲い・空調・温度を各run manifestへ保存する。B3は温度値または計測不能理由を要求する。
- 詳細手順は`PLAN_B_ADDITIONAL_MEASUREMENT_JP.md`。Plan B、HF/step、capture、auto、workflow対象テスト80件成功。実機、PAT、カメラ、記録、重い後処理は実行していない。
- テストのmock sessionが誤って`stereo_acoustools_3d_records_auto/auto_recording_session_20260826_121054.json`を1件作成した。`run_dir`は空で生イベントはなく、実機も開いていないが、成果物保全規則に従い削除していない。

## 2026-08-21: Tier E全6 run完了、Tier F一括計測へ進行可

- 中断後の再開session `auto_recording_session_20260821_180652.json`は`complete`で、未取得だったE1/Y r02とE2/Z r01/r02の3本がすべて成功した。先行成功分と合わせTier EのX/Y/Z各r01/r02、全6 runが揃った。
- 全6 runで`capture_complete=true`、左右Master/Slave同期、左LED検出、20.5秒共通記録区間、左右NPZ保存が正常。イベント上限未到達、`processing_status=pending`。
- 先に確認済みのE0/X r01に加え、残り5 run・80ジャンプの直後5--35 msと保持後半850--900 msを軽量イベント像で確認した。最大`±1.70 mm`を含む全窓で左右両眼に粒子像が残った。Tier E全6 run・96ジャンプで脱落・片眼ロストは見当たらない。
- 粒子追跡・三角測量・応答振幅/減衰の定量解析は未実施。生存・可視性と記録品質の確認に基づき、Tier FのX/Y/Z 3 runを同一PAT/OpenMPDセッションで連続計測可能。

## 2026-08-21: ステップ応答の保持限界探索Tier F/G/Hを追加

- Tier Eより先の保持限界探索として、`step_response_identification_tier_f/g/h_plan.json`と対応するexportを追加した。FはX/Y/Z各1 runの`±1.80/±1.90 mm`（最大88.6%境界）、Gは軸・符号別6 runの`2.00/2.05/2.10 mm`（最大98.0%）、Hは軸・符号別6 runの`2.15/2.20/2.30 mm`（100.3--107.3%）である。
- F/G/Hは振幅順をランダム化せず弱い順にした。G/Hは符号を別runにして、最初の脱落で反対方向の情報まで失わないようにした。全holdは1.0秒、Fは10.0秒・8ジャンプ、G/Hは8.0秒・6ジャンプ。
- 生成・hardware loadの通常staircase安全検査は推定脱出境界2.144 mm以上を引き続き拒否する。Tier Hだけはplan内の明示的opt-inで最大2.30 mmまで許可し、metadataへ境界超過を記録する。一般のstaircaseには開放していない。
- auto入口へE→F、F→G、G→Hの生存ackを追加した。Hはさらに`--acknowledge-step-response-escape-boundary-probe`を要求し、1 hardware sessionにつき厳密に1 runだけに制限する。各runのpreview/Enter、`--keep-going`禁止、異常時打切りは維持。
- F 3、G 6、H 6の計15 exportを生成し、順序・振幅・境界比・previewを確認した。全Tierのhardware-free dry-runに成功。HF/auto、安全ゲート、長パス・capture保存の対象テストは合計47件成功（HF一式33件、capture/NPZ 14件）。実機はこの追加機能では開いていない。
- 詳細と段階実行コマンドは`STEP_RESPONSE_ESCAPE_BOUNDARY_JP.md`。作業時点でTier E残り5 runのauto session `auto_recording_session_20260821_170206.json`は`running`、2/5 run完了であり、その成果物には触れていない。Tier FはE全runの生存確認後のみ開始する。

## 2026-08-21: Tier E0成功、残り5 runの同一セッション連続計測対応

- `step_response_identification_000_E0_x_staircase_be6eea9bd4_20260821_165259`を実測・確認した。短縮runディレクトリでWindows長パス問題を回避し、auto sessionは`complete`、`capture_complete=true`、試行1回目で保存成功となった。
- 左右Master/Slave同期、共通20.5秒記録区間、左LED検出、PAT送信、左右NPZ保存が正常。左右ともイベント上限未到達で、18.0秒軌道の全区間が収録されている。
- 全16ジャンプについて、直後5--35 msと1.0秒保持後半850--900 msを軽量イベント像で確認した。最大`±1.70 mm`を含む全窓で左右両眼に粒子像が残り、脱落・片眼ロストは見当たらない。粒子追跡・三角測量・応答振幅/減衰の定量解析は未実施。
- Tier Eの1 hardware session 1 run制限を解除した。残り5 runを同一PAT/OpenMPD接続で連続計測できる。各run前のステレオpreview/Enter確認、中心復帰、`--keep-going`禁止、異常・拒否・Ctrl+C時の即時打切りは維持する。
- 残り5 runの実行コマンドを`STEP_RESPONSE_MEASUREMENT_JP.md`へ追加した。

## 2026-08-21: Tier E0の記録保存失敗とWindows長パス対策を拡張

- 3回目の`E0_x_staircase_boundary_long_hold_r01`はプレビューPNG生成を通過したが、約20.5秒後にステレオ記録子プロセスが終了コード1となった。`auto_recording_session_20260821_164234.json`は`status=failed`で、成功したTier E runはまだ0件。失敗runは通常ポリシーにより削除済みで、E0の粒子生存性を評価できるデータはない。
- 長いrunディレクトリ名の下では、`_capture_attempt_01/stereo_recording/...`の左右NPZ・metadataパスと原子的保存用一時NPZパスもWindowsの従来上限付近または上限超過になることを確認した。
- `stereo_acoustools_3d_recording_core.py`で、完全なlabelをmanifestへ保持したまま、最深のcapture metadataと衝突回避suffixを含む絶対パスが248文字以内になるようrunディレクトリ名を自動短縮するようにした。
- `eventcam_npz_storage.py`の原子的保存用一時名を短い`.events.<pid>.tmp`へ変更した。通常名・長いrun名・NPZ保存を含む対象テスト41件が成功し、E0のhardware-free dry-runも成功。実機は開いていない。
- Tier Eの1 hardware session 1 run制限は維持している。まずE0/X r01を再取得し、保存完了と全16ジャンプの粒子生存を確認してから、残り5 runの一括計測対応へ進む。

## 2026-08-21: Tier E0開始前のWindows長パス失敗を修正

- `E0_x_staircase_boundary_long_hold_r01`を2回開始したが、両方とも粒子運動・カメラ記録の開始前に失敗した。成功したE runデータはまだない。通常ポリシーにより未成功runディレクトリは削除され、failedのauto session JSON 2件だけが監査記録として残る。
- 原因は長いE run名と生成プレビュー名の組合せで絶対パスが265文字になり、Windowsの従来パス上限を超えたこと。Dの対応パスは256文字だった。ログのLibUSB警告後も左右とも`camera opened`となっており、今回の停止箇所はその後のプレビューPNG保存である。
- `stereo_acoustools_3d_recording_core.py`へ、生成する`*_trajectory_preview.png`と`*_ideal_log.csv`のstemを絶対パス248文字以内へ自動短縮する処理を追加した。runディレクトリ名、label、元command artifact、manifestは維持する。
- 長いパスと通常パスの回帰テストを追加し、capture/HF対象テスト36件成功。実機は開いていない。同じE0/X r01コマンドを再実行可能。

## 2026-08-21: Tier D全6 run完了、Tier E進行可

- 同一hardware sessionでTier Dの残り5 run（D0/X r02、D1/Y r01/r02、D2/Z r01/r02）を実測した。sessionは`complete`で、全runが試行1回目に`capture_complete=true`となった。
- 5 runすべてで左右Master/Slave同期、共通14.1秒記録区間、左LED検出、保存、PAT送信が正常。イベント上限未到達で、LED基準の11.6秒軌道終了後に約1.10秒の記録余裕がある。失敗試行ディレクトリは残っていない。
- 残り5 run・160ジャンプの各々について、直後5--35 msと保持後半180--210 msを軽量イベント像で確認し、全窓で左右両眼に粒子像が残っていた。先に確認済みのD0/X r01を合わせ、Tier D全6 run・192ジャンプで脱落・片眼ロストは見当たらない。
- Tier Eの最初のE0/X r01へ進行可能。粒子追跡・三角測量・応答振幅/減衰の定量解析は未実施。Tier Eは最大1.70 mm（推定脱出境界の79.3%）かつ長保持のため、1 runずつ進める制限を維持する。

## 2026-08-21: Tier D残り5 runの同一セッション連続計測対応

- ユーザー要望により、Tier Dの「1 hardware sessionにつき1 run」制限を解除した。複数のD labelを1回のPAT/OpenMPD接続で連続計測できる。
- staircase各run直前のステレオプレビューとEnter確認、中心復帰、`--keep-going`禁止、失敗・拒否・Ctrl+C時の即時打切り、Tier C生存ackは維持した。Tier Eの1 session 1 run制限は変更していない。
- D0/X r01を除く残り5 runの連続計測コマンドを`STEP_RESPONSE_MEASUREMENT_JP.md`へ追加した。

## 2026-08-21: Tier D最初のX run確認

- `step_response_identification_000_D0_x_staircase_challenge_r01_scale100_20260821_155918`を実測・確認した。`capture_complete=true`、左右Master/Slave同期、共通14.1秒記録区間、左LED検出、保存が正常で、イベント上限未到達。LED基準の11.6秒軌道終了後にも約1.11秒の記録余裕がある。
- 全32ジャンプについて、ジャンプ直後5--35 msと保持後半180--210 msの軽量イベント像を確認した。最大`±1.25 mm`を含む全窓で左右両眼に粒子像が残り、脱落・片眼ロストは見当たらない。粒子追跡・三角測量・応答振幅/減衰の定量解析は未実施。
- Tier Dの残り5 runへ進行可能。複数runを同じhardware sessionで連続実行できるが、毎回previewとEnter確認を行い、異常時は後続runへ進まず停止する。

## 2026-08-21: Tier C完了、長保持Tier E準備

- Tier Cの6 run・全192ジャンプを確認し、左右両眼で粒子生存、左右同期、LED検出、完全な記録区間、保存成功を確認した。応答振幅・減衰の定量解析は未実施だが、Tier Dへ進行可能。
- `step_response_identification_tier_e_plan.json`と`step_response_identification_tier_e_export/`を追加。X/Y/Z各2 run、`±1.25/1.40/1.55/1.70 mm`、最大は推定脱出境界の79.3%。全振幅が推定力最大点約1.072 mmを越える高リスク診断。
- Tier Eは減衰尾部観察のため、ターゲット保持・中心復帰後保持をともに1.0秒とした。全ジャンプ間隔は正確に1.0秒。近境界曝露を抑えるため各run内反復は1回で、各runは18.0秒・16ジャンプ。独立反復はr01/r02で確保する。
- staircase metadataをTier Eまで対応させ、`--acknowledge-step-response-tier-d-survived`を追加。Tier Eも1 hardware sessionにつき厳密に1 runだけで、複数選択はPAT接続前に停止する。
- Tier Eの6 run export、正確な指令レベル、非対象軸ゼロ、1.0秒ジャンプ間隔、hardware-free dry-runを確認。対象回帰テスト58件成功。実機・PAT・カメラは開いていない。
- Tier Eの実測はTier D全6 run・192ジャンプの生存確認後のみ。詳細は`STEP_RESPONSE_MEASUREMENT_JP.md`。

## 2026-08-21: Tier B完了、Tier C/D準備

- Tier Cの`C0/C1/C2`各r01/r02、合計6 runを実測・確認した。すべて`capture_complete=true`、`processing_status=pending`、左右同期・共通記録区間・LED検出・保存が正常で、イベント上限未到達。
- 各runの32ジャンプ後を軽量イベント像で確認し、全192ジャンプで左右両眼に粒子イベント塊が残っていた。脱落・片眼ロストは見当たらず、Tier Dの最初の1 runへ進行可能。粒子追跡・三角測量・応答量解析は未実施。
- Tier Bの`B0/B1/B2`各r01/r02、合計6 runを確認した。すべて`capture_complete=true`、`processing_status=pending`、左右Master/Slave同期・共通記録区間・LED検出・保存が正常で、イベント上限未到達。
- 各runの24ジャンプ後を軽量イベント像で確認し、全144ジャンプで左右両眼に粒子イベント塊が残っていた。粒子追跡・三角測量・重い後処理は実行していない。Tier Cへ進行可能。
- `step_response_identification_tier_c_plan.json`と`step_response_identification_tier_c_export/`を追加。X/Y/Z各2 run、合計6 run、各11.6秒・32ジャンプ、`±0.30/0.60/0.85/1.05 mm`、最大は推定脱出境界の49.0%。Tier B生存ack付きで全6 runを1セッション選択可能。
- `step_response_identification_tier_d_plan.json`と`step_response_identification_tier_d_export/`を追加。X/Y/Z各2 run、`±0.70/0.95/1.15/1.25 mm`。最大1.25 mmは推定脱出境界の58.3%で、1.15/1.25 mmは推定力最大点約1.072 mmを越える高リスク領域。
- staircase metadataをTier Dまで対応させ、`--acknowledge-step-response-tier-c-survived`を追加。当初の1 hardware session 1 run制限は、その後D0/X r01の生存確認とユーザー要望を受けて解除した。runごとの強制checkpointと異常時打切りは維持する。
- C/D各6 runのexport生成、正確な指令レベル・非対象軸ゼロ、hardware-free dry-runを確認。対象回帰テスト54件成功。実機・PAT・カメラは開いていない。
- Tier C一括計測とTier D段階実行コマンドは`STEP_RESPONSE_MEASUREMENT_JP.md`。

## 2026-08-21: Tier A計測確認とTier B準備

- `stereo_acoustools_3d_records_step_response/`のA0/X、A1/Y、A2/Zを確認した。3 runとも`capture_complete=true`、左右Master/Slave同期済み、共通記録区間完全、イベント上限未到達、LED検出成功、`processing_status=pending`。
- 各runの16ジャンプ後を軽量イベント像で確認し、合計48ジャンプすべてで左右両眼に粒子イベント塊が残っていた。粒子追跡・三角測量・重い後処理は実行していない。
- `step_response_identification_tier_b_plan.json`を追加。Tier Bだけを有効化し、X/Y/Z各2 run、合計6 runとした。各runは9.2秒、24ジャンプ、最大0.70 mm（推定脱出境界の32.7%）。Tier A/Cは含めない。
- `step_response_identification_tier_b_export/`へ6 runを生成した。名前は`B0_x_staircase_ladder_r01/r02`、`B1_y_staircase_ladder_r01/r02`、`B2_z_staircase_ladder_r01/r02`。
- 全6 runのhardware-free dry-runに成功。まず`B0_x_staircase_ladder_r01`だけを実機計測し、生存・両眼可視を確認してから残り5 runへ進む。
- `B0_x_staircase_ladder_r01`を2026-08-21に実測・確認済み。`capture_complete=true`、左右同期・共通記録区間・LED検出は正常、イベント上限未到達。24ジャンプすべての直後に左右両眼で粒子イベント塊を確認し、脱落・片眼ロストは見当たらなかった。Tier Bの残り5 run（X r02、Y r01/r02、Z r01/r02）へ進行可能。
- Tier B実機には`--acknowledge-step-response-risk`と`--acknowledge-step-response-tier-a-survived`の両方が必要。staircase固有のpreview/Enter強制と`--keep-going`禁止は維持。
- Tier B専用plan/export検証を追加し、対象テスト47件成功。実機・PAT・カメラは開いていない。
- 詳細コマンドは`STEP_RESPONSE_MEASUREMENT_JP.md`。

## 2026-08-21: 階段状トラップジャンプ計測

- `20260820/`のSINDy自由応答同定資料を現行ステレオ3D自動計測へ読み替えた。
- 古い引き継ぎの「NPZ再生ドライバ未実装」は、現行`acoustools_stereo_eventcam_3d_recording_auto.py --hf-export-dir`がすでに満たすため、この経路へ`kind=staircase`を接続した。
- `hf_identification_trajectory.py`へ、中心→ターゲット→中心の不連続step、正確なhold、順序seed、jump時刻metadata、専用previewを追加した。
- staircaseは連続軌道の速度・加速度scaleを通さず、最大オフセット、最大ジャンプ、推定脱出境界2.144 mmで生成時とhardware load時の両方を検査する。資料同梱版に残っていたload時の通常微分制限への逆戻りも修正した。
- `step_response_identification_plan.json`を追加。Tier AはX/Y/Z各1 run（0.25/0.40 mm、各6.8 s）を有効、Tier B/Cは生存確認まで無効。
- auto実機入口に`--acknowledge-step-response-risk`、B/C段階確認を追加。staircaseは各runでステレオpreviewとEnterを強制し、`--keep-going`を禁止する。通常HFの50% chirp確認とは分離した。
- Master/Slave serial、左LED ROI、校正、10 kHz PAT更新、記録/manifest coreは変更していない。粒子抽出は接続していない。
- Tier Aの3条件を`step_response_identification_export/`へ生成し、preview-onlyとdry-runに成功。実機は開いていない。
- 全Tierのrun数・時間・jump数とTier A NPZの正確な16段差を検証し、対象回帰テスト51件成功。
- 手順は`STEP_RESPONSE_MEASUREMENT_JP.md`。

## 2026-08-19: 粒子逸脱runの隔離

- ユーザーが粒子逸脱を確認したindex 025、026、027、042、044、045、046、047を`stereo_acoustools_3d_records_particle_deviations/`へ移動した。
- index 025には2回分の取得があるため、移動したrunディレクトリは合計9件、約3.075 GB（2.864 GiB）。計測成果物は削除していない。
- 移動元のauto session JSONは取得時の監査記録として保持し、記録済みの旧絶対パスは書き換えていない。
- `stereo_acoustools_3d_postprocess_core.py`に、記録されたideal logの絶対パスが移動により無効な場合、現在のrunディレクトリ内の同名ファイルへフォールバックする処理を追加した。
- 隔離先の`README.md`に全run、元ディレクトリ、失敗データとしての注意事項、後処理例を記録した。
- 対象テスト43件成功。実機・カメラ・PATは開いていない。重い後処理も実行していない。

## 現在の計測方針

- 実機計測と重い後処理は分離する。ユーザーが明示しない限りPAT、カメラ、recording入口を実行しない。
- カメラは左`00000508` Master、右`00000509` Slave。
- PAT開始LEDは左、ROI `600,0,1280,180`。
- ステレオ校正は`stereo_checkerboard_calib_extrinsics_20260805/stereo_calibration_square7p12_extrinsics_final.npz`。
- 計測成果物は削除・上書きしない。

## 2026-08-18: 拡張3D・チャープデータセット

- `Extended_Random_3D`、JSONパラメトリック形状、train/validation/diagnostic roleをauto版へ実装済み。
- サンプルは`extended_3d_dataset_plan.json`、説明は`EXTENDED_3D_DATASET_JP.md`。
- 新たに`Chirped_3D`を実装。XYZ別の振幅、開始・終了周波数、位相、線形／対数掃引、包絡、振幅変調を設定できる。
- 通常計画は`chirped_3d_dataset_plan.json`（9条件）。
- 保持限界計画は`chirped_3d_retention_boundary_plan.json`（4条件）。
- 詳細手順は`CHIRPED_3D_DATASET_JP.md`。
- `risk_level=retention_boundary`は`--acknowledge-extended-trajectory-safety`に加え、
  `--acknowledge-retention-boundary-risk`がない限りPATを開く前に停止する。
- 単独計画では保持限界4条件を一括実行せず、`--start-index N --limit 1 --confirm-each-run`で弱い順に実行する。
  統合計画の末尾に限り、専用tailモードで各条件のpreviewとEnter確認を強制しながら連続実行できる。
- 対話ステレオ版mode 17、カメラなし版mode 7でも`Chirped_3D`を選択可能。
- ハードウェアなしdry-runとpreview生成のみ実施済み。実機は開いていない。

## 検証状態

- 対象テスト43件成功。
- 通常9条件、保持限界4条件のdry-run成功。
- preview CSV上の`amplitude_scale_applied`は全条件1.0。
- プレビュー:
  - `chirped_3d_dataset_plan_preview/trajectory_overview.png`
  - `chirped_3d_retention_boundary_plan_preview/trajectory_overview.png`
- 保持限界版の実測最大値は概ね、boundary01で1,340 mm/s・0.71e6 mm/s²、
  boundary04で4,406 mm/s・4.65e6 mm/s²。後者は粒子脱落を想定し得る診断条件。

## 2026-08-18: カスプ軌道データセット

- ユーザーの意図はチャープではなくカスプだったが、チャープ実装は有用なため保持。
- `Cusped_3D`を追加。cardioid、nephroid、deltoid、astroid、3--12尖点hypocycloidに対応。
- `plane=xy/xz/yz`、平面回転、第3軸持ち上げ、`rounding`によるsharp／rounded対照を指定可能。
- 第3軸持ち上げも尖点で速度0になるため、数学的カスプを3Dで保持する。
- 通常計画`cusped_3d_dataset_plan.json`はtrain 9、validation 3の計12条件。
- 保持限界計画`cusped_3d_retention_boundary_plan.json`は4条件。
- 最終保持限界は10尖点hypocycloid、基本25 Hz、最高幾何周波数250 Hz、
  約4,015 mm/s、6.23e6 mm/s²。必ず弱い順に1条件ずつ実行する。
- 単独計画のretention_boundary入口は、専用ackに加えて選択1条件と`--confirm-each-run`を要求する。
  統合計画の末尾に限り、専用tailモードで各条件のpreviewとEnter確認を強制しながら連続実行できる。
- 説明とコマンドは`CUSPED_3D_DATASET_JP.md`。
- 対話ステレオ版mode 18、カメラなし版mode 8。
- dry-runとpreviewのみ実施。実機は開いていない。
- 対象テストは43件成功。

## 直近のDelay A/B結果

- `full_3pairs`ではdelayモデルにより3D RMSEが平均約24.7%悪化。
- 現在のハート10 Hzは20/30/40 Hz高調波を含み、旧random3Dの最大16 Hzでは不足していた可能性がある。
- チャープ／拡張3Dデータセットは、この帯域不足と軸間結合を補うための追加同定計画。

## 2026-08-18: 追加3D軌道の統合auto計画

- `all_additional_3d_dataset_plan.json`を追加。include型で元JSONの更新を自動反映する。
- 条件0--39は通常40条件: extended 19、chirped 9、cusped 12。
- 条件40--47は保持限界8条件: cusped 4の後、指定されたchirped 4を最後に配置。
- 合計48条件、train 28、validation 12、diagnostic 8。指令運動時間合計228.22秒。
- run metadata/preview CSVへ`plan_group`, `source_plan`, `source_json_index`を追加。
- `--allow-retention-boundary-tail`を追加。複数保持限界は末尾連続区間だけ許可し、
  各条件でpreviewとEnterを強制する。保持限界選択時は`--keep-going`禁止。
- 統合手順は`ALL_ADDITIONAL_3D_DATASET_JP.md`。
- 統合dry-runとpreview生成は成功。実機は開いていない。
- 対象テスト43件成功。

## 2026-08-19: 低リスクwideランダム条件

- `extended_3d_dataset_plan.json`末尾へ5--50 Hz midbandのwide条件4本を追加。
- XY、YZ、X単軸、Z単軸について元条件と同じseedを使い、対応比較可能。
- 指定範囲は±1.5 mm、加速度上限は65,000 mm/s²、周波数上限50 Hz、`risk_level=standard`。
- 4条件とも自動縮小なし。最大速度177--290 mm/s、最大加速度45,286--61,351 mm/s²。
- Y単軸の元条件はすでに約−1.50～+1.36 mmのため重複追加していない。
- `EXTENDED_3D_DATASET_JP.md`と統合計画の件数・通し番号を更新。
- extended/統合previewと48条件dry-runに成功。対象テスト43件成功。実機は開いていない。

## 2026-08-19: ホログラム生成進捗の間引き

- `compute_holograms_for_positions()`の進捗表示を10点ごとから10,000点ごとへ変更。
- 総点数10,000以下では`[PREP] hologram N/total ...`と完了時間の表示を省略する。
- 総点数10,000超では10,000、20,000、...点で経過時間と平均時間を表示する。
- 実機は開かず、純粋判定関数を追加。対象テスト43件成功。
- `--role train --role validation --dry-run`で保持限界を除く40条件の選択を確認済み。

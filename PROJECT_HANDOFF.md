# Project handoff

## 2026-09-23: 15 Vの熱の保持試験を準備（電圧ラベル・音を出した時刻基準の5分ごとkcheck・記録用紙の下書き）

- **依頼元**: 解析側（Macの「OptiTrap→PINNいけるか」セッション）からの連絡 `G:\マイドライブ\Experiment\20260921\MESSAGE_TO_ACQUISITION_PC_20260923.md`（09-23 00:05。セッション間の直接送信が届かなかったためG:に置かれた）と正本 `measurement_plan/THERMAL_PLAN_20260922.md` §3。18 V・4.8 Aでは約30分でリセッタブルヒューズが働き、熱の定常状態が無いため、15 Vで保持試験（セッション1: 5分ごとにkcheck x/y/zを60分）→ 動作点での取り直し（セッション2）を行う。ユーザーの依頼で準備した。
- **自動計測の入口**（`acoustools_stereo_eventcam_3d_recording_auto.py`）に追加: `--supply-voltage-v V`（export runのみ。全runの `run_label` に `_V15` を付けてフォルダ名を `..._scale100_V15_<時刻>` にし、`automation.supply_voltage_V`・`run_label_tag`、session JSONの `supply_voltage_V` に記録。0 < V <= 30）。`--schedule-interval-sec S`／`--schedule-group-size N`（無人モードのみ。組g（g>=1）の最初のrunを、PATの出力開始＝音を出した時刻 `active_output_started_wall_ns` から g×S 秒まで待たせる。組0はcheckpoint直後。遅れた組は待たずに開始。待機中は中心で保持＝稼働。待機中のCtrl+Cは `interrupted`・終了コード130）。各runに `sound_on_since`、`seconds_since_sound_on_at_run_start`、`schedule_group`、`scheduled_group_offset_sec` を記録。**電圧タグがWindowsのパス長制限でフォルダ名から切れる場合は、dry-run・実機とも開始前に拒否する**（最初の試作では `stereo_acoustools_3d_records_thermal` 下でkcheckのフォルダ名が短縮され `_V15` が消えることをdry-runで発見して対処した）。
- **一括実行**: 新規 `run_thermal_hold_test.ps1`（`-SupplyVoltage` 必須、`-DurationMin` 60、`-IntervalMin` 5、`-Axes` x,y,z、`-Unattended`、`-DryRunOnly`。ff_heartのexportのkcheckを使い、planと不一致なら再生成。13組・39 run。ディスク空きが「1.6 GB×run数＋20 GB」未満なら開始前に停止）。既定の出力先は電圧ごとの `stereo_acoustools_3d_records_V15`（18 Vの記録と混ぜないという計画書の指示どおり。この長さならタグは切れない）。`run_ff_heart_validation.ps1` に `-SupplyVoltage`（指定時は出力先の既定も `..._V15`）と、kcheck x/y/zだけを表す設計名 `K` を追加（セッション2の `-Designs "K,WXZ,WYZ,WXY,WXZP4,WXZM4"` を1回のPATセッションで撮るため）。
- **記録用紙**: 新規 `thermal_log_prefill.py`。session JSONから `thermal_log_template.csv` と同じ13列のCSV（utf-8-sig）を作り、記録開始時刻（runフォルダの時刻）・run名・音を出した時刻・電圧・「group N, t+X min」を埋める。電流・温度・湿度・粒子交換は手で記入。既存ファイルは上書きしない。
- 手順書 `THERMAL_15V_MEASUREMENT_JP.md` を追加し、`STEREO_ACOUSTOOLS_3D_AUTO_JP.md` に節を追加。セッション2の手順3以降（新しいk・γで作り直したFF設計と `_Ws`）は解析側から届いてから取り込む。
- 検証: PowerShell 5.1で `run_thermal_hold_test.ps1 -SupplyVoltage 15 -DryRunOnly`（13組×x,y,z＝39 run、最後の組60分、短縮警告なし、C:の空き131 GBに対し必要約62 GB）、`run_ff_heart_validation.ps1 -Designs "K,WXZ,WYZ,WXY,WXZP4,WXZM4" -SupplyVoltage 15 -DryRunOnly`（13 run）、長い出力先でのタグ切れの拒否を確認。dry-runで出力先は作られない。新規 `test_thermal_hold_test` 7件（電圧タグの書式、組の待ち判定、偽の時計で「確認に2分かかっても組1・2がちょうど5分・10分に始まる」こと、全runのタグとメタデータ、遅れた組はすぐ始まること、不正オプションでハードウェアを開かないこと、タグ切れの拒否、記録用紙の下書きと上書き防止）を含む対象スイート197件が成功。`miyabi_upload.stamp()` は新しいフォルダ名からも時刻を取れる。PAT・カメラは開いていない。コミット・pushは行っていない。
- **解析側への回答**: AcousToolsの音速は **346 m/s**（このvenvが読む `inifinicam_control\AcousTools-main\...\Constants.py` の `c_0 = 346`、λ = 8.65 mm。記録パイプラインで上書きしている箇所は無い）。解析側の「343 m/sに固定」という前提と違う。
- **照合**: 解析側のリポジトリ `OnoderaHisato/PINN-project`（`hstonodera` から移管、非公開、コミット1件 09-23 00:18、`pinn_optitrap/` 232ファイル）とG:の `Experiment\20260921`（345ファイル）を比べた。共通168ファイルは全件内容一致（89件は改行コードのみの違い。このPCのgitは `core.autocrlf=true`）。リポジトリだけにあるのはPINN・同定モデル本体（`pinn_optitrap.py`、`optitrap_model.py`、`real_vzr_fit.py`、結果・図）、G:だけにあるのは指令データ（npz・csv・npy）と `MESSAGE_TO_ACQUISITION_PC_20260923.md`・`UPDATES_20260921.md`。**計測PC宛ての連絡はG:にしか無い。** 複製はスクラッチ領域のみ。
- **訂正**: 09-21の項に「Miyabi-Gは未検証」と書いたが、解析側が `/work` にARM環境を作って検証済みだった（8 sの記録1本で2D追跡が完全一致、3D差1.7e-13 mm、699 s。`qsub -q short-g`、`module load python/3.11.15`）。CとGの同時実行枠は別勘定。
- **`miyabi_upload.py` が古かった（ユーザーの指摘で確認）**: 09-21 01:26に写した版は `PBS = "run_ff_heart_post_list.pbs"`（2D追跡→3D化まで）だったが、G:の版（`Experiment\20260920` と `20260921` の `handoff_acquisition_pc`、PINNリポジトリも同一）は09-21 03:06に `run_post_and_compare_list.pbs` へ更新されていた。新しいPBSは3D化のあと `run_ff_heart_compare_list.sh` で、固定変換 `camera_to_pat_pooled_ffheart_20260918.npz`（後処理が使う `camera_to_pat_pooled_2s_20260916.npz` とは別）による指令 u との比較（`ideal_comparison_3d`）と、FF runでは所望軌道 r との比較（`reference_comparison_3d`）まで行う。手元の `miyabi_upload.py` と `REFERENCE_ACQUISITION_PC_UPLOAD.md` をG:の現行版に置き換え（SHA-256一致。旧版はスクラッチ領域に退避）、`MIYABI_UPLOAD_JP.md` のジョブ行と役割分担（同じrunをSSD経由で送らない、解析側へ一覧ファイル名を伝える）を更新。`test_miyabi_upload` 8件成功。`miyabi_upload_missing.py` は `miyabi_upload` の送信処理を使うので自動的に新しいPBSになる。
- 09-21の15 runは古いPBS（とその写しのprepost用スクリプト）で処理したが、05:15--05:16に解析側が `ffheart_20260918` の変換で比較を作り直していた（全15本の `ideal_comparison_3d` と、FF 3本の `reference_comparison_3d`）。09-21の項の3D RMSE（CWxz 1.056 など）とSSDの `ideal_comparison_3d` はこの作り直し後のもので、値は全15本でMiyabiと一致。不足していたFF 3本の `reference_comparison_3d`（各21 MB）をSSDへ追加した（`tar -k` で既存ファイルは上書きしていない）。所望軌道 r に対する3D RMSEは CWxz 0.886、OFFW2 0.954、CW2 0.848 mm。
- 未確認の仮説: 2026-09-16の「PAT接続から約16〜17分でLEDが消え、左右のイベントが激減」は、18 Vでヒューズが働いてPATが止まった可能性がある（手順書に記載）。

## 2026-09-21: 面外yの比較3本＋kcheck 12本を計測、Miyabiで3D化、SSDへ複製

- **計測**: 09-21 02:52--03:05に15 run。`ffheart_s7_f7_CWxz`（034）、`_OFFW2`（035）、`_CW2`（036）の3本を、x／y／zのkcheck（各4本、計12本）で挟んで記録した。`OFFWxz`（033）は撮っていない。合計23.0 GB。
- **転送**: PuTTYの接続共有に相乗りする `miyabi_upload_missing.py --plink x10733@miyabi-g.jcahpc.jp` で15 runを送信し、全runでサイズ照合`OK`。
- **3D化の経路（新規）**: Miyabi-Cはグループ上限が「同時実行2・受理4」で待ちが出るため、**Prepostキュー**を使った。Prepostは3ノード・1ジョブ1ノード・walltime最大6時間・240 GiB・1ユーザー1ジョブで、**トークン係数0（無課金）**、ログインノードと同じx86環境。**対話ジョブ専用**で、バッチ投入は `Job has no interactive requested` で拒否される。予約は利用支援ポータルの「プリポスト予約」のみ。
- **対話ジョブを接続から切り離す方法**: ジョブの中で `nohup` しても、PBSは対話セッションの終了時にジョブごと落とすので効かない。**`qsub -I` のクライアント側をログインノードのtmuxに入れる**と切断に耐える。出力をファイルへリダイレクトするとTTYが無くなり `standard input and output must be a terminal for interactive job submission` で拒否されるため、端末のまま起動し `tmux pipe-pane -o` でログを別に落とす。起動用の `prepost_launch_c.sh` と処理本体 `prepost_post_c.sh` はMiyabiの `/work/.../stereo_3d/` に置いた。
- **分散**: 当初は15 runを1つのprepostジョブ（`xargs -P 15`、112コア）で処理したが、22分で2D追跡が8/30本という進みで、walltime 3時間に対して余裕が無かった。04:10--04:12に前夜のCジョブ2本が完走してCの枠が空いたので、**15 runを重複の無い3リスト（各5 run）へ分割**し、Miyabi-Cのバッチ2本（`post_a` 3410509、`post_b` 3410510、`run_ff_heart_post_list.pbs`、NPAR=5）とprepostの対話1本（3410511、tmux `postc`）を3ノードで並走させた。04:16開始、05:08／05:08／05:12完了（**約56分**）。分割前のジョブ3410441は停止し、約22分ぶんをやり直した（`tracking_resume_checkpoint.json` が0/30で再利用できるものが無かったため `--resume` は使っていない）。
- Miyabi-Gは使っていない。aarch64で `module load python/3.11.15` とユーザー領域のcv2が要り、**試験ジョブの出力が残っておらず未検証**のままなので、実績のあるC側を選んだ。
- **結果**: 15 runすべてが `3D OK`、Tracebackやエラー行は0。3D点はハート各135,000点、kcheck各195,000点で欠けなし。理想軌道比較は固定の camera-to-PAT 登録による絶対比較（`spatial_alignment=fixed`）で、重なりはハート79,999点、kcheck 139,999点。3D誤差RMSEはハートが `CWxz` 1.056、`OFFW2` 0.981、`CW2` 1.041 mm、kcheck 12本が0.364--0.608 mm（軸別RMSEは x 0.127--0.759、y 0.152--0.592、z 0.170--0.636 mm）。
- **SSD**: `D:\stereo_acoustools_3d_records_ff_heart` に生データ15 run（21.4 GB）と、3D点群・理想軌道比較・左右2D追跡CSV（1.9 GB）を複製した。15/15 runで `stereo_3d_points.npz`・`stereo_ideal_comparison.npz`・左右の `event_centres_interp.csv` がそろっていることを確認。フォルダ合計177.1 GB、D:の空き1,258 GB。
- PAT・カメラは開いていない。計測成果物の削除・上書きはしていない。コミット・pushは行っていない。

## 2026-09-21: Miyabiへ未アップ分を転送（PuTTYの接続に相乗り）、次の計測はexport済み

- **転送経路**: 計測PCから `miyabi-c` へ直接sshすると、公開鍵は受理されるがサーバーが `keyboard-interactive`（ワンタイムパスワード）を要求する。端末の無い実行環境からは入力できず、加えて短時間に接続を繰り返すと `Not allowed at this time` で接続自体を拒否される。**PuTTYの接続共有に相乗りする方法で解決した**: 保存セッション `miyabi-g`（`miyabi-g.jcahpc.jp`、鍵は `OneDrive\Documents\miyabi.ppk`）の `ConnectionSharing` を1にしてPuTTYでログインしておくと、`plink -share -batch x10733@miyabi-g.jcahpc.jp <command>` が追加の認証なしで通る（標準入力のtarもそのまま流れる）。`/work` は両ログインノードで共通。
- 追加したもの: `miyabi_upload.py`（G:の手順書からの写し、SHA-256一致。変更していない）、差分だけを送る `miyabi_upload_missing.py`（`--plink USER@HOST` で相乗り、`--remote-list`、`--dry-run`、`--no-qsub`）、OTP入力用の小窓 `miyabi_askpass.ps1`／`.cmd`（相乗りが使えないとき用。`SSH_ASKPASS_REQUIRE=force` で使う）、手順書 `MIYABI_UPLOAD_JP.md`、G:手順書の写し `REFERENCE_ACQUISITION_PC_UPLOAD.md`、テスト `test_miyabi_upload.py`（8件）。`~/.ssh/config` に `Host miyabi-c.jcahpc.jp` を作成した（鍵はエージェント）。
- **転送結果**（すべてサイズ照合 `OK`）: ff_heartの未アップ6本（kcheck x／y／z、09-20 23:38--23:43）を送り3D化ジョブ `3409984.opbs` を投入。リモートが145 MB少なかった `..._kcheck_z_S105_6jumps_scale100_20260920_233705` を送り直し（ジョブ `3409987.opbs`）。8月17日のHF 13本（11.3 GB）も送ったが、**ユーザーから「8月のは不要」との指示があった**（送信完了後）。ジョブは投入していないので計算資源は使っていない。`/work/xg25g006/x10733/eventcam/stereo_3d/stereo_acoustools_3d_records_hf` は、ユーザーの判断でMiyabi上に残す（2026-09-21に確認。3D化ジョブは投入していない）。large_step 8本、step_response_2s 18本、vzr_step 6本は元からアップ済みだった。
- 注意: 3D化が済んだrunはリモートの方がファイルが増えてサイズが大きくなる。転送不足の判定は「リモートが**小さい**run」で行うこと（単純な不一致では判定できない）。Miyabiの `/work` 使用量は396.6 GB（ユーザー個人の上限は設定なし）。
- **次の計測**: G:の `20260920/HANDOFF.md`（09-21 01:15）と `w_field/W_FIELD_3D_PLAN.md` の「次に撮るもの」は `heart_s7_f7_CW2`／`OFFW2`／`CWxz` と `wscan_XZ_a/b` で、いずれも取り込み済み。w走査XZのa/bは09-20 23:53--23:54に記録済みなので、残るのは前者3本。09-20/09-19のG:フォルダにある4本の指令は同一（SHA-256一致）で、新しい指令は増えていない。本番exportをplanに合わせて再生成し、37 runでplanのSHA-256と一致、新4本はG:とビット一致、`-Designs "CWXZ7,OFFW2,CW2"` のdry-run（15 run、PAT再生192 s、最初の1本だけcheckpoint）が成功した。対象テスト50件が成功。PAT・カメラは開いていない。コミット・pushは行っていない。

## 2026-09-21: 計測PCからMiyabiへ直接転送する経路を用意（鍵の登録は未了）

- G:の `Experiment\20260920\measurement_plan\handoff_acquisition_pc`（09-21 01:14--01:15）の手順に従い、`miyabi_upload.py` をこのフォルダへ写した（SHA-256一致）。手順書の写しは `REFERENCE_ACQUISITION_PC_UPLOAD.md`、日本語の実行手順は `MIYABI_UPLOAD_JP.md`。従来の「計測PC → SSD → Mac → Miyabi」をやめ、run を tar+gzip で ssh の標準入力へ流し、`/work/xg25g006/x10733/eventcam/stereo_3d/<recordsフォルダ>/<run>` へ展開、`chgrp -R xg25g006`・`chmod g+s`、サイズ照合、一覧作成、`qsub -q regular-c ... run_ff_heart_post_list.pbs` までを1回のsshで行う（1バッチ最大9 run＝2段階認証も9 runに1回）。
- このPCの状況: `ssh` はOpenSSH 10.3で利用可能。**Miyabi用の鍵（`~/.ssh/id_ed25519_miyabi`）と `~/.ssh/config` はまだ無い**。鍵の作成とポータルへの登録はユーザー作業（`MIYABI_UPLOAD_JP.md` の1章）。実機のssh転送はまだ試していないので、最初は小さいrun 1本を `--no-qsub` で送ること（手順書の指示。Windowsでの実行と2段階認証の入力は未検証）。
- 検証（sshもネットワークも使わない）: `--dry-run` が動作（`stereo_acoustools_3d_records_large_step` で8 run・7.2 GB、feedforward優先→時刻順の並び）。新規 `test_miyabi_upload.py` 8件: run名の時刻抽出、`._*` を除いたサイズ集計、`--since`／`--match` の絞り込みと並び、対象なしの終了、**Windowsのパス区切りがtar内で `/` になること**と `._*` の除外、リモートコマンド（置き場所・グループ・一覧・qsub・NPAR・PBS名）、`--no-qsub`、サイズ不一致の検出、9 runごとのバッチ分割と一覧名。`miyabi_upload.py` 自体は変更していない。
- 計測データは読み取りのみで、転送・削除は行っていない。PAT・カメラは開いていない。コミット・pushは行っていない。

## 2026-09-21: 面外yの扱いを比べる4本（OFFWxz／CWxz／OFFW2／CW2）を追加

- G:の `Experiment\20260919\measurement_plan`（09-21 00:17〜00:29更新）から、7 Hzのハート4本を取り込んだ。`heart_s7_f7_OFFWxz`／`heart_s7_f7_CWxz`（w の面外y成分を補償しない）と `heart_s7_f7_OFFW2`／`heart_s7_f7_CW2`（yの細かい成分を共振応答から逆算した値に差し替え。`w_field/make_w_y_dynamic.py`）。`source/` へ複製（SHA-256一致）し、planの末尾へ `ffheart_s7_f7_OFFWxz`（index 033）、`ffheart_s7_f7_CWxz`（034）、`ffheart_s7_f7_OFFW2`（035）、`ffheart_s7_f7_CW2`（036）を内容ハッシュ固定で追加した（合計37 run）。`feedforward_design` に `w_axes`（xz／xyz）と `w_y_source` を記録する。
- 背景（G:のREADME、9/20 22:40--22:58の実測）: 面内の補償は効いた（xの共振帯 C 0.241 → CW 0.088 mm、面内の再現誤差0.17 mmで非再現の床0.25 mmより下）。一方、面外yの共振帯は OFFW 0.24 → 0.40、CW 0.17 → 0.74 mmと悪化した。ゆっくり回して測った w のyの細かい成分（波数9--13、約0.05 mm）が測定の偏りで、y共振（Q≈10）で増幅されたため。35 Hz以下のyは下がっているので、なだらかな成分は本物。
- 制限内（速度524--534 mm/s、加速度63,557--66,212 mm/s²、\|u−r\| 0.323／0.698／0.465／0.770 mm）。yの指令はxz版が0、2版が最大0.346 mm。一括スクリプトに設計名 `OFFWXZ7`、`CWXZ7`、`OFFW2`、`CW2` を追加した。計画書の指示どおり、4本は同じsessionで休止とkcheckの直後に続けて撮る。
- ユーザーのlarge_step session（09-21 00:36開始）が動いていたため、FFのexportは再生成していない。作業用フォルダへ37 runのexportを作って確認した: 既存33本の `command_trajectory.npz` はバイト単位で同一、新4本はG:の配列とビット一致、`axis` はxz／xz／xyz／xyz、フォルダ名は短縮されない、dry-run（新既定の「最初の1本だけcheckpoint」）成功、PowerShellの構文検査も通る。**次に `run_ff_heart_validation.ps1` を起動したときに本番exportが自動で作り直される。**
- テスト: `test_ff_heart_validation` を37 runへ更新（xz版はyが0で `w_axes=xz`、2版はyを動かす、各\|u−r\|）。対象スイート182件が成功（公式exportの読み込み1件はskip）。PAT・カメラは開いていない。コミット・pushは行っていない。
- 計測状態（JSONのみの軽量確認）: 大振幅ステップは09-21 00:36--00:43に全8本が記録された。`auto_recording_session_20260921_003608.json`（XL12、XL15）と `..._003927.json`（YL12、YL15、XL18、YL18、XL20、YL20）はどちらも `complete`、全run試行1回目で成功、失敗0、LED警告0（peak 1708--2564）、`processing_status=pending`、ホログラムは位置3種類。2.0 mmのXL20／YL20まで到達している（粒子の保持は各previewの目視のみ。NPZは開いていない）。新しい既定（最初の1本だけcheckpoint）で実行された。
- 未取り込み: `w_acoustic/`（計算によるwの予測、計測不要）、`ku/`、`ff_heart/cardioid/`。`w_field/` の段階3以降（3面の場、探り信号つきの速い記録）は指令がまだ無い。

## 2026-09-20: 大振幅1軸ステップ（large_step）を追加、run間確認の既定を「最初の1本だけ」へ変更

- **large_step**: G:の `Experiment\20260919\measurement_plan\large_step`（`NEXT_MEASUREMENTS_20260920.md` の3番。水平の飽和長 vxr を分離して力の上限 A_r = k/vxr を出す）を `large_step_20260920/` に統合した。plan `large_step_plan.json`（8 run: `largestep_XL12/XL15/XL18/XL20` と `largestep_YL12`〜`YL20`。1軸だけを0→+S→0→−S→0、3周・12ジャンプ、保持0.4 s（両端0.5 s）、13保持、5.4 s、54,000点）、計画書と生成器の写し（SHA-256一致）、`export_large_step/`、`reference_comparison_20260920.json`。既存の単軸 `kind=staircase`（`amplitudes_mm`＋`directions`＋`repeats_within_run`）でそのまま作れるので生成器は変更していない。8本とも解析側 `commands/vzrstep_*L*.npz` とビット一致。
- tierは1.2 mm→D、1.5→E、1.8→F、2.0→G（脱出境界2.144 mmの56／70／84／93%）。`+S → −S` の2Sジャンプは無いので1ジャンプは常にS。各runの `safety_limits` は設計値+0.05 mmに固定。一括実行 `run_large_step_all.ps1`（既定 `-Runs "XL12,XL15"`、`-ConfirmEachRun`、`-Unattended`、`-CommandScale`、`-CaptureTailMarginSec` 既定5、`-KeepFailedCaptures`、`-Regenerate`、`-OutputDir` 既定 `stereo_acoustools_3d_records_large_step`、`-DryRunOnly`）と手順書 `LARGE_STEP_MEASUREMENT_JP.md` を追加、README・自動計測ガイド・`.gitignore` を更新した。**小さい段から上げ、戻りが鈍ったら止める**（1.8／2.0 mmは推定力最大点より外から戻る）。
- **run間確認の既定を変更（ユーザー要望）**: `acoustools_stereo_eventcam_3d_recording_auto.py` に `--prompt-on-capture-failure` を新設した。`--unattended-after-first-checkpoint` と併用すると、自動撮り直し（`automatic_capture_retries`）を使わず `None` のままにするので、記録・LED検出の失敗時は記録コアの「同じ軌道を再計測しますか？ (Y/n)」が出る。`--automatic-capture-retries` との併用と、無人モードなしの指定は開始前に拒否する。session JSONへ `prompt_on_capture_failure` を記録する。
- 一括スクリプト3本（`run_ff_heart_validation.ps1`、`run_vzr_step_all.ps1`、`run_large_step_all.ps1`）の既定を「最初のrunだけステレオpreviewとEnter、以後は自動で開始。失敗時だけY/n」に変更した。`-ConfirmEachRun` で従来の全run確認へ戻せる。`-Unattended` は「失敗時も尋ねず自動で撮り直す」の意味に変わった（従来は無人モード全体を意味していた）。vzrの正弦波（`run_vzr_identification_all.ps1`）は仕様どおり全run確認のままで、multitoneは無人モードを拒否する。
- 検証: PowerShell 5.1で `run_large_step_all.ps1 -DryRunOnly`（既定2 run、tier D/E）、`-Runs "XL18,YL20" -ConfirmEachRun -DryRunOnly`、`-ConfirmEachRun -Unattended` と不正run名の拒否を確認。既定の出力先でフォルダ名は短縮されない。新規 `test_large_step_plan` 8件（解析側生成器とのビット一致、段・tier・保持数、制限超過の拒否、フォルダ名、実機読み込み、dry-runとack不足の拒否、`--prompt-on-capture-failure` で最初の1本だけcheckpoint・`automatic_capture_retries=None`・session JSONの記録、自動撮り直しとの排他、`-ConfirmEachRun` 相当の全run確認）を含む対象スイート182件が成功（公式FF exportの読み込み1件は、exportがplanより古い間はskip）。`test_plan_b_measurement` は `plan_b_20260825/` が無いため除外。
- 実機のsession（09-20 22:33開始のFF 7 Hz w補償）が動いていたため、FFのexportは再生成していない。**次に `run_ff_heart_validation.ps1` を起動したときにplanとの不一致を検出して自動で作り直される**（w走査10本を含む33 run）。PAT・カメラは開いていない。コミット・pushは行っていない。
- 未取り込み: `w_acoustic/`（計算による w の予測、計測不要）、`ku/`、`ff_heart/cardioid/`。

## 2026-09-20: 外乱 w の面走査（wscan、渦巻き10本）を追加

- G:の `Experiment\20260919\measurement_plan\w_scan`（09-20 00:37）の渦巻き指令10本を取り込んだ。中心→半径9.2 mm→中心の渦巻き、1.5回転/s、14 s・140,000点、補償なし（u=r）。最大87 mm/s・818 mm/s²（制限の11%・0.8%）で共振は立たず、粒子が w をそのままなぞる。面はXZ（y=0）・YZ（x=0）・XY（z=0）が半径9.2 mm、y=±4 mmへずらしたXZが半径8 mm（最大オフセット8.856 mm）。各面にa（反時計回り）とb（時計回り）があり、w が位置だけの関数か進む向きにも依るかを判定する（`NEXT_MEASUREMENTS_20260920.md` の2番）。
- `source/` へ複製（SHA-256一致）し、planへ `wscan_XZ_a`〜`wscan_XZym4_b`（index 023〜032、`duration_sec: 14.0`、内容ハッシュ固定、`feedforward_design.design=WSCAN`、`plane`・`direction`・`radius_mm`・`plane_offset_mm` を記録）を追加した。y=±4 mmの2面は最初の0.5 sでyへ移り最後に戻るので、どのrunも中心から始まり中心で終わる（`max_endpoint_offset_mm` は0）。
- `run_ff_heart_validation.ps1` に設計名 `WXZ`／`WYZ`／`WXY`／`WXZP4`／`WXZM4` を追加した。1つ選ぶとその面のa→bを続けて記録する。計画書のとおり**w走査の前後にはkcheckを入れない**（ハートの設計と混ぜた場合、ハート側には従来どおり入る）。全runでpreviewとEnterは従来どおり。
- ユーザーの実機session（09-20 22:33開始、`export_ff_heart` を使用）が動いていたため、本番exportは再生成していない。作業用フォルダへ33 runのexportを作って確認した: 既存23本の `command_trajectory.npz` はバイト単位で同一、新10本はG:の配列とビット一致、`axis` はxz／yz／xy／xyz、フォルダ名は短縮されない、dry-run成功、PowerShellの構文検査も通る。**次に `run_ff_heart_validation.ps1` を起動したときに、planとの不一致を検出して本番exportが自動で作り直される。**
- テスト: `test_ff_heart_validation` を33 runへ更新（走査の軸・半径・中心始終点、u=r、速度と加速度が制限に対して十分小さいこと、`command_differs_from_reference` はOFFとWSCANだけfalse）。FF 21件を含む対象スイート174件が成功（公式exportの読み込み1件は、exportがplanより古い間はskip）。`test_plan_b_measurement` は `plan_b_20260825/` が無いため今回も除外した。PAT・カメラは開いていない。コミット・pushは行っていない。
- 未取り込み: 同じ更新に含まれる `large_step/`（水平の力の上限。1軸ステップ1.2〜2.0 mm、既存のステップ経路で流せる）、`w_acoustic/`（音場計算によるwの予測、計測なし）、`ku/`、`ff_heart/cardioid/` は依頼がないため手を付けていない。

## 2026-09-20: 最新モデルのw補償ハート（OFFW／CW、7 Hz・10 Hz）と7 HzのCを追加

- G:の `Experiment\20260919\measurement_plan\ff_heart`（09-20 00:02更新）から、`heart_s7_f7_OFFW`（u=r−w）、`heart_s7_f7_CW`（u=C−w）、`heart_s7_f7_C_delay_inverse`（CW7の比較相手）、`heart_s7_f10_OFFW`、`heart_s7_f10_CW` を `source/` へ複製し（SHA-256一致。同名ファイルは20260918側と同一）、planの末尾へ `ffheart_s7_f7_OFFW`（index 018）、`ffheart_s7_f7_CW`（019）、`ffheart_s7_f7_C_delay_inv`（020。フォルダ名の短縮を避けるため "inverse" を略した）、`ffheart_s7_f10_OFFW`（021）、`ffheart_s7_f10_CW`（022）を内容ハッシュ固定で追加した。w は1〜2 HzのOFFで測った平衡点のずれ場で（README「1 Hz / 2 Hz の補償なし」）、3次元なのでW指令にはyの成分（最大0.465 mm）がある（`axis=xyz`）。w は面ごとに違うのでYZ面版は作っていない。
- 制限: 7 Hzの3本は既定の制限内（加速度76,030／74,601／46,763 mm/s²、\|u−r\| 0.564／0.834／0.479 mm）。**10 HzのOFFW／CWは加速度104,193／106,815 mm/s²で既定の100,000を超えるため、READMEの指示（上限を110k以上へ）に従い、この2本だけ `safety_limits` の `max_acceleration_mm_s2` を110,000にした**（他の制限は既定のまま。\|u−r\| 0.393／0.886 mm）。一般の既定値は変えていない。
- `run_ff_heart_validation.ps1` に設計名 `OFFW7`、`CW7`、`C7`、`OFFW10`、`CW10` を追加した。`-Designs "OFF7,OFFW7,C7,CW7,OFFW10,CW10" -NoSandwich -DryRunOnly` でexportを自動再生成し、dry-run成功。23 runのexportのうち既存18本の `command_trajectory.npz` は変化なし、新5本の指令・所望軌道・時間配列はG:とビット一致、フォルダ名は短縮されない。READMEと `make_ff_heart.py` の写しを09-19 23:59版へ更新し、`make_w_compensation.py` の写しを追加した。
- テスト: `test_ff_heart_validation` の公式plan・ソース検査を23 runへ更新（W指令のyの成分、10 Hzだけ加速度上限が110,000でそれ以外は既定と同じこと、10 Hzの2本は実際に100,000を超えること）。FF／vzr／HF／auto／workflow／capture／LED／syncの対象テストは成功。`test_plan_b_measurement` の4件は、`plan_b_20260825/` と `stereo_acoustools_3d_records_plan_b*` がこのフォルダから無くなっている（ユーザーが容量確保のため移動したとみられる。git上は `plan_b_20260825/` の追跡ファイル6本が削除扱い）ため失敗し、今回の変更とは無関係。PAT・カメラは開いていない。コミット・pushは行っていない。
- 容量: 09-19 18:30にCドライブの空きが0.22 GBまで減り、kcheck_xの記録が `No space left on device` で失敗した（session `..._182256`）。その後ユーザーが容量を空け、09-20時点の空きは252 GB。1 runの左右NPZは約1.5 GB。

## 2026-09-19: 周回1 Hz・2 HzのOFF、YZ面のOFF、kcheck_yを追加、ステップの既定をx／y／zへ

- G:の `ff_heart` に04:35追加された `heart_s7_f1_OFF.npz`／`heart_s7_f2_OFF.npz`（u=r、7 mm、**14 s**・140,000点、最大速度74.5／149.0 mm/s、最大加速度934／3,736 mm/s²。README「次の計測」: 共振で増幅されない速さで平衡点のずれ w(u) を直接なぞらせる）を `source/` へ複製し（SHA-256一致）、planへ `ffheart_s7_f1_OFF`（index 010）・`ffheart_s7_f2_OFF`（011）を `duration_sec: 14.0` と内容ハッシュ固定で追加した。ユーザーの指示は「考えすぎずシンプルに追計測」。G:の `cardioid/`（16:03追加）と `OPTITRAP_END_TO_END.md` は依頼外のため取り込んでいない。
- **YZ面（ユーザー要望、計画書にはない）**: `hf_identification_trajectory.py` の `kind=imported` に `swap_axes`（2軸の列を入れ替える）を追加した。内容ハッシュの固定と検査は入れ替え前のG:ファイルに対して行い、入れ替え後の配列のハッシュを `positions_sha256`、元のハッシュを `source_positions_sha256` としてmetadataへ記録する。所望軌道 r も同じく入れ替え、時間配列はそのまま。実機読み込み時の再検査は従来どおりexportの配列のハッシュで行う。未指定時の挙動は不変。planへ `ffheart_s7_f1_OFF_yz`、`f2`、`f5`、`f7`、`f10_OFF_yz`（index 013〜017、`swap_axes: ["x", "y"]`）を追加した。速度・加速度・オフセットはXZ面と同じ。
- **kcheck_y**: `kcheck_y_S105_6jumps`（index 012、kcheck_xと同じ仕様でy軸。exportはkcheck_xのx列とy列を入れ替えたものと一致）を追加した。y方向1.05 mmのステップは9/16のS105_yで保持実績がある。
- `run_ff_heart_validation.ps1`: 設計名 `OFF1`、`OFF2`、`OFF10`（= `OFF`）と `OFF1YZ`〜`OFF10YZ` を追加。**`-SandwichAxes` の既定を `x,z` から `x,y,z` へ変更**（ユーザー要望。従来に戻すには `-SandwichAxes "x,z"`）。14 sのハートをPAT再生時間の表示に反映。警告の閾値は、y追加でハート2本が11 runになるため「9 run以上」から「13 run以上」へ変えた。
- planは手で整形された書式を保ったまま末尾へ追記した（差分は `purpose` の1行と追加222行）。exportの再生成は、17:24にユーザーのsessionが始まっていたため、まず作業用フォルダの一時exportで検証し、sessionの終了（17:30、下記）後に `-Designs "OFF1,OFF2" -DryRunOnly` で本番exportを自動再生成した。18 runのexportは検証済みの一時exportとバイト単位で同一、既存10本の `command_trajectory.npz` は変化なし。取り込み・YZの計15本はG:の配列（YZは列を入れ替えたもの）とビット一致（`reference_comparison_20260918.json` を更新）。dry-runは成功し、フォルダ名は短縮されない（最長の `feedforward_validation_017_ffheart_s7_f10_OFF_yz_scale100_<時刻>`）。
- 注意: このsessionのコード変更（`hf_identification_trajectory.py` 17:25、plan 17:26、スクリプト17:27）は17:24開始のsessionより後で、実行中のプロセスは古いコードとexportを読み込み済みのため影響はない。
- テスト: `test_ff_heart_validation` に `swap_axes` の単体テスト（列の入れ替え、r も入れ替わること、固定ハッシュが元ファイルを守ること、不正指定の拒否、exportと実機読み込み）を追加し、公式plan・ソースの検査を18 run（kcheck 3本、14 sのrun、YZと元のXZが列の入れ替えで一致すること）へ更新した。対象スイート計181件が成功（skipなし）。`FF_HEART_VALIDATION_MEASUREMENT_JP.md` を更新した。PAT・カメラは開いていない。コミット・pushは行っていない。
- 計測状態（JSONのみの軽量確認）: `auto_recording_session_20260919_022431.json`（02:24〜02:38、`complete`）でOFF5・OFF7をx／zのステップで挟んだ8本を記録済み。kcheck zの1本目（02:26）は1回目の試行が記録プロセスの失敗で、対話の撮り直しで成功した（`capture_report.failures` に1件）。LED peakは2044〜3116、警告なし。`auto_recording_session_20260919_172416.json`（17:24〜17:30）は `failed`、最初のkcheck_xが終了コード1・エラー文字列なし・runディレクトリなしで終わっている（ユーザーがpreviewで中止したもの。2026-09-19にユーザーが確認）。
- 未取り込み: G:の `ff_heart/cardioid/`（16:03追加、`cardioid_a5p4_f10_{OFF, A_delay, C_delay_inverse, OT_ident_nodelay}`。`make_ff_heart.py --shape cardioid --scale 5.4`、10 Hz）はplan・source・exportのいずれにも入っておらず、計測もしていない（記録フォルダ名に `cardioid` を含むのは2026-08-19のカスプ軌道データセットの5本で別物）。metrics.jsonでは最大速度658〜679 mm/s、最大加速度64,072〜73,387 mm/s²、\|u−r\|最大0.70 mmで、既存のFFハートの制限（800 mm/s、100,000 mm/s²、1.0 mm）に収まる見込み。

## 2026-09-19: 周回5 Hz・7 Hzの補償なしハート（OFF5／OFF7）を追加、ステップ型vzrの実測6本を確認

- G:の `20260918\measurement_plan\ff_heart` に00:38追加された `heart_s7_f5_OFF.npz` と `heart_s7_f7_OFF.npz`（README「追加した指令（2026-09-19）」。10 Hzのハートで見えた70〜90 Hz残差の加振源を切り分けるための、周回周波数だけを変えたu=r）を取り込んだ。どちらも80,000点・10 kHz・8 s・XZ面、経路と大きさは10 HzのOFFと同じ（最大オフセット9.154 mm）、最大速度372.5／521.5 mm/s、最大加速度23,352／45,770 mm/s²で、既存の制限（800 mm/s、100,000 mm/s²）に収まる。`source/` へ複製（SHA-256一致）し、planの末尾へ `ffheart_s7_f5_OFF`（export index 008）と `ffheart_s7_f7_OFF`（index 009）を内容ハッシュ固定で追加した。`feedforward_design.design` は `OFF`（u=r）のまま、`loop_frequency_hz` に5／7を記録する。READMEの写しも01:32版へ更新した（生成器 `make_ff_heart.py` は変更なし）。
- planは手で整形されたJSON（配列を1行に書く形式、CRLF）なので、既存部分をバイト単位で保ったまま新しい2本を同じ書式で挿入した（差分は `purpose` の1行と追加52行だけ）。作業中に一度、plan全体を `json.dumps` で書き直してしまったが（シェルのheredocで `\\n` が潰れて修正パッチが効かなかったため）、バックアップから復元して挿入し直した。内容は同一であることを確認済み。
- `run_ff_heart_validation.ps1` に設計名 `OFF5`、`OFF7` を追加し、再生成時に必要なソース一覧へ2本を加えた。実行中のsessionがないことを確認したうえで `-Designs "OFF5,OFF7" -DryRunOnly` を実行し、「exportがplanより古い」の自動再生成が端から端まで動くことを確認した（10 run出力、dry-run成功、フォルダ名の短縮なし）。再生成後、既存8本の `command_trajectory.npz` は00:00のsessionで使ったexportとバイト単位で同一、取り込み8本の指令・所望軌道・時間配列はG:のNPZとビット一致（`reference_comparison_20260918.json` を更新）。
- 撮るときのコマンドは `run_ff_heart_validation.ps1 -Designs "OFF5,OFF7"`（既定どおりx／zステップで挟む8 run、PAT再生100 s）。ハート2本だけなら `-NoSandwich` を付ける。解析側への注意: `evaluate_ff_heart.py` と `analyze_ff_final.py` はフォルダ名の `_OFF_` で設計を判別し、周回周波数を `F = 10.0` に固定しているので、この2本はフォルダ名の `s7_f5`／`s7_f7`（またはmanifestの `loop_frequency_hz`）で区別して周波数を切り替える必要がある。
- テスト: `test_ff_heart_validation` の公式plan・ソースの検査を10 runへ更新（新しい2本は速度がf、加速度がf²に比例して10 HzのOFFより小さいことも検査）。対象スイート計180件が成功（skipなし）。`FF_HEART_VALIDATION_MEASUREMENT_JP.md` の設計表・オプション・例を更新した。PAT・カメラは開いていない。コミット・pushは行っていない。
- 計測状態の更新（JSONのみの軽量確認）: ユーザーがステップ型vzrを実測した。`stereo_acoustools_3d_records_vzr_step/auto_recording_session_20260919_015001.json`（01:50〜01:54、`complete`）で XS1 → XS2 ×3、`..._015500.json`（01:55〜01:57、`complete`）で XS3 → YS2。6本とも全runでpreviewとEnter、試行1回目で成功、失敗0・LED警告0、`processing_status=pending`。LED peakは1817〜3136、onsetの見込みとの差は+5.3〜+5.4 msで一定、ホログラムは位置7種類。フォルダ名は `step_response_identification_00N_vzrstep_<段>_scale100_<時刻>` で短縮なし。粒子の生存確認は各previewの目視のみで、NPZは開いていない。5 Hz・7 HzのOFFはまだ計測されていない。

## 2026-09-19: ステップ型vzr計測（水平1軸とzの同時ジャンプ）を統合、00:00のA03／C03 sessionを確認

- G:の `20260918\measurement_plan\VZR_MEASUREMENT_PLAN.md` が2026-09-19 00:43に追記され（正弦波の指令は取得側の加速度上限で弾かれるので、(A)上限を400kへ上げるか、(B)xとzの同時ステップにする）、新しい計画 `vzr_step/VZR_STEP_PLAN.md` が置かれた。これを `vzr_step_20260919/` に統合した。plan `vzr_step_plan.json`（6 run: `vzrstep_XS1/XS2/XS3/YS1/YS2/YS3`、水平±0.6/0.8/1.0 mm＋z ±0.4/0.5/0.6 mm、31保持・30ジャンプ（単独6＋6、同時18）、保持0.3 s、9.7 s、97,000点）、計画書2本と生成器の写し（SHA-256一致）、`export_vzr_step/`、`reference_comparison_20260919.json`。exportの6本は解析側 `commands/vzrstep_*.npz` の `positions_mm` と全サンプルでビット一致（-0.0なし）。
- `hf_identification_trajectory.py` の `kind=staircase` に `level_sequence_mm`（保持位置を `[x, y, z]` で並べた1ブロック。`repeats_within_run` 回繰り返す）を追加した。ブロックは中心で終わること、同じ位置を続けないこと、`amplitudes_mm`／`directions`／`order_seed` と併用しないこと、`axis` を書くなら動かした軸と一致することを検査する。`axis` は動かした軸の連結（`xz`、`yz`）、metadataに `axes`、detailに `levels_xyz_mm`・`jump_kinds`・`jump_kind_counts` を持つ。安全検査は従来どおり振幅のみ（中心からの距離・1サンプルの変化・脱出境界2.144 mm、いずれもベクトルの大きさ）で、同時ジャンプは √(S_h²+S_z²) で判定される。各runの `safety_limits` は設計値のすぐ上（0.73／0.95／1.17 mm）に固定。tierはS1／S2がC、S3（1.166 mm）がD（解析側の試作は全C。tier Hだけが特別扱いなので実行時の差はない）。previewは多軸のとき軸ごとに描く（従来は `AXIS_INDEX["xz"]` でKeyErrorになる箇所だった）。
- **単軸staircaseは不変**: パッチ後の生成器で `ff_heart_plan.json` を作業用フォルダへ再生成し、現行 `export_ff_heart` の8 run（kcheck x／zを含む）と `command_trajectory.npz` がバイト単位で同一、`trajectory_metadata.json` も同一であることを確認した。loader（`stereo_acoustools_3d_hf_common.py`）、auto入口、記録coreは変更していない。
- 一括実行 `run_vzr_step_all.ps1` を追加した（export生成／planと不一致なら自動再生成→dry-run→実機）。既定は計画書どおり `-Runs "XS1,XS2,XS2,XS2"`、全runでpreviewとEnter。`-Runs`（XS1〜YS3、繰り返し可）、`-CommandScale`、`-Unattended`、`-AutomaticCaptureRetries`、`-CaptureTailMarginSec`（既定5）、`-KeepFailedCaptures`、`-Regenerate`、`-OutputDir`（既定 `stereo_acoustools_3d_records_vzr_step`）、`-DryRunOnly`。実機ackは `--acknowledge-step-response-risk`。計画書の「10分以上の休止」と「run間隔一定」はスクリプトでは計らない（未実装。間隔は `-Unattended` でほぼ一定になる）。手順は `VZR_STEP_MEASUREMENT_JP.md`。自動計測ガイド・README・`VZR_IDENTIFICATION_MEASUREMENT_JP.md` に追記、`.gitignore` に `vzr_step_20260919/` のpy/md/jsonを許可（exportは除外）。
- 計画側への回答になる確認事項: (1) 解析側の試作export `vzr_step/export_vzr_step` は、取得PCの現行ローダのdry-runをそのまま通る（2軸同時の変化も `axis: "xz"` も受理。作業用フォルダへの写しで確認）。`--stagger-ms 1` などの回避策は不要。(2) 追記の「100,000 mm/s²で弾かれる」は `kind=imported`（FFハート）の上限で、9/17に整備した正弦波のvzr plan（`kind=multitone`）は上限400 mm/s・400,000 mm/s²なので全指令が通る。`run_vzr_identification_all.ps1` は今も実行できる（選択肢(A)は実施済みの状態）。正弦波・ステップのどちらもまだ計測されていない。
- 検証: PowerShell 5.1で `run_vzr_step_all.ps1 -DryRunOnly`（既定4 run）、`-Runs "XS3,ys2" -CommandScale 0.5 -Unattended -DryRunOnly`、不正なrun名の拒否を確認。既定の出力先でフォルダ名は短縮されず、dry-runで出力先は作られない。`test_vzr_step_plan` 新規13件（解析側生成器の写しとのビット一致、保持配置、同時ジャンプのベクトル判定、脱出境界、不正指定の拒否、単軸の不変、実previewとexport・実機読み込み、フォルダ名、dry-run、ack不足と `--keep-going` の拒否、繰り返しlabelの順序と全runのcheckpoint、無人モード）を含む対象スイート計180件が成功（skipなし）。実機入口をackなしで直接起動する確認は行っていない（ackの拒否はモックで確認）。PAT・カメラは開いていない。コミット・pushは行っていない。
- 計測状態の更新（JSONのみの軽量確認）: `auto_recording_session_20260918_232104.json` は `complete`（8 run）。1〜3本目（kcheck x `..._232144`、kcheck z `..._232252`、OT-ident `..._232934`）は前項のとおりPAT開始時刻が誤り（`suspicious=true`）で、LED復旧後の4〜8本目（OT-ident `..._233747` を含む）は警告なし。`auto_recording_session_20260919_000003.json`（`-Designs "A03,C03"`、00:00〜00:14）も `complete`: kcheck x／z → A03 `feedforward_validation_006_ffheart_A_delay03_scale100_20260919_000702` → kcheck x／z → C03 `feedforward_validation_007_ffheart_C_delay03_inv_k918_scale100_20260919_001101` → kcheck x／z の8 runが全て1回目で成功、失敗0、LED警告0、`processing_status=pending`。exportは起動時（09-18 23:59）に自動で再生成されていた。`reference_comparison_20260918.json` を更新した: 取り込み6本の指令・所望軌道・時間配列はG:のNPZとビット一致、kcheck x／z・OFF・A・Cの `command_trajectory.npz` は `export_ff_heart_used_20260918_2013_to_2134` とバイト単位で同一。
- 未対応: G:に `measurement_plan/ku/`（KU計測計画）が置かれているが、依頼がないため取り込んでいない。記録プロセスのハングの原因は未特定のまま。

## 2026-09-18: LED置き直し後の確認、23:21のsessionの3本はPAT開始時刻が誤り、遅延0.3 msの設計2本を追加

- ユーザーがLEDの配置を見直し、検出できるようになったと報告。`auto_recording_session_20260918_232104.json`（23:21開始、`-Designs "OTI,OTI"`、全runでEnter、確認時点で5/8）を確認した。**1〜3本目はLEDが写る前の記録で、PAT開始時刻が誤っている**（いずれも受理されたが警告2件つき、`pat_start_led.consistency.suspicious=true`）: kcheck x `..._232144`（見込みから−1079 ms）、kcheck z `..._232252`（−1563 ms、その前にLED未検出で3回撮り直し）、**OT-ident `feedforward_validation_005_ffheart_OT_ident_nodelay_scale100_20260918_232934`（−743 ms、peak 46／閾値46）**。4本目 kcheck x `..._233045` は3回のLED未検出の後、置き直したLEDで検出（peak 1349／閾値45、見込みから+7.1 ms、警告なし）。5本目 kcheck z `..._233457` も正常（peak 1072、+7.0 ms）。したがって最初のOT-identは解析に使えず、撮り直しが必要（同sessionの6本目のOT-identはLED復旧後）。成果物は変更していない。
- 5本目の1回目の試行は、記録プロセスが親の待ち時間60.5秒でも終了せず `TimeoutExpired`。対話モードの撮り直し確認（前々項の(3)）により2回目で成功し、sessionは継続した。一方、`--stream-stall-timeout-sec 3` つきで起動していたのに記録プロセスは数秒で終了しておらず、**配信停止の監視（同(2)）はこのハングを捉えていない**。ハングは「記録中にsliceが来ない」状態ではなく、保存区間の後（連結・NPZ保存・カメラ解放）か記録プロセス本体にある可能性がある。失敗試行は削除済みで、コンソール出力も未入手のため未特定。次は `-KeepFailedCaptures` で失敗試行の `*_recording_meta.json` と `stereo_recording_manifest.json`（`stream_stall_watchdog`）を残すか、コンソール出力を確認する。
- G:の `20260918\measurement_plan\ff_heart` に22:46追加された遅延0.3 msの2本を取り込んだ（ユーザーのメッセージは `20260917` を指していたが、そちらに更新はなかった）。`heart_s7_f10_A_delay03.npz`（u=r(t+0.3 ms)、\|u−r\| 0.224 mm）と `heart_s7_f10_C_delay03_inverse_k918.npz`（先読み0.3 ms＋逆モデル、k x 0.25／z 3.45、γ x 0.050／z 0.022、\|u−r\| 0.455 mm）を `source/` へ複製（SHA-256一致）。planの末尾へ `ffheart_A_delay03`（index 006）と `ffheart_C_delay03_inv_k918`（index 007。フォルダ名が短縮されないよう run名の "inverse" を略した。設計名は `feedforward_design.design` に完全な形で残る）を内容ハッシュ固定で追加した。どちらも既存の制限内（速度745／716 mm/s、加速度93,401／92,704 mm/s²）。READMEと生成器はG:側で更新されていなかった。
- 実行中のsessionのexportを書き換えないよう、exportの再生成はその場では行わず、一括スクリプトに「export manifestの `source_plan_sha256` がplanのSHA-256と違えば起動時に自動で再生成する」判定を追加した（一致している間は書き換えない）。設計名 `A03`、`C03` を追加。検証は作業用フォルダの一時exportで行った: 新2本の指令・所望軌道・時間配列はG:のNPZとビット一致、既存6本の `command_trajectory.npz` は現行exportとバイト単位で同一、既定の出力先でフォルダ名は短縮されない、dry-run成功。スクリプトは構文検査と判定部分だけを確認した（実行するとexportを書き換えるため）。`reference_comparison_20260918.json` は次回の再生成後に更新する必要がある。
- 解析側の `evaluate_ff_heart.py` は22:43に更新され、フォルダ名の前方一致（`_A_delay`、`_C_delay`、`_OT_ident` など）で設計を判別する。短縮されたCのフォルダ名にも一致する。新しい2本は従来のA／Cと同じラベルになるので、時刻かmanifestの設計名で区別する必要がある。
- テスト: 対象スイート計167件が成功（公式exportの読み込みテスト1件は、exportがplanより古い間はskipする形にした）。PAT・カメラは開いていない。コミット・pushは行っていない。

## 2026-09-18: LED検出不能の原因はLEDが左カメラの視野から外れたこと（判定条件ではない）

- 既定を警告のみに戻した後も、ユーザーから「やはりLED検出できない」との報告。`auto_recording_session_20260918_232104.json`（23:21開始）の1本目 `..._kcheck_x_S105_6jumps_scale100_20260918_232144` は終了コード0で保存されたが、新設の警告が2件付いた（onsetが見込みから−1079 ms、peakが閾値の1.00倍: onset 0.587秒、見込み1.666秒、peak 45／閾値45）。21:21・21:22と同じ、ノイズ1ビンの誤検出である。`pipeline_manifest.json` の `pat_start_led.consistency.suspicious=true`。
- 左NPZを時間窓だけmemory-mapで読んで確認した。正常なrunでは、LEDは左画像の**最上端**（x≈760--820、y≈0--40。フレームの上端で一部切れている）に写り、PAT開始の瞬間にその領域だけイベントが急増する（22:08: 常時約6万イベント/秒 → 開始の100 msで11.4万。20:14: 常時3--4千 → 7千）。23:21のrunでは、同じ領域は3--4千イベント/秒のまま変化がなく、常時あった輝点も消えている。PAT開始の前後50 msを画面全体で比べても、局所的に増えた場所はROIの内外どこにもない（増えたのは粒子位置だけ）。つまりLEDの点滅は左カメラに写っていない。目視では点灯していても、LEDかカメラのわずかな位置ずれで上端から外れたとみられる。変化が起きたのは22:51:54（厳しい判定のまま成功）と22:53:38（検出失敗が続く）の間。
- 影響: 23:21以降にこの状態で保存されたrunは、PAT開始時刻が誤っている（警告つき）。解析で0.8 ms程度の遅延を論じる用途には使えない。LEDを直して撮り直す必要がある。OT-identの有効な記録はまだない。
- 対処（ユーザーの作業）: LEDを左カメラの視野の内側、ROI `600,0,1280,180` の中で上端から十分離れた位置（例 y≈60--120）に写るよう置き直し、previewで確認する。ROIやカメラ設定は変更していない。確認用の図は共有済み（作業用フォルダ、リポジトリ外）。
- コードの変更はない。PAT・カメラは開いていない。NPZは読み取りのみで、計測成果物は変更していない。

## 2026-09-18: LED整合チェックの既定を「拒否」から「警告のみ」へ戻す、失敗理由をsession JSONへ記録

- 前項(1)の導入後、ユーザーから「LEDは目視で点灯しているのに検出されなくなった。(1)は余計だったかもしれない」との報告。記録を確認した。`auto_recording_session_20260918_224835.json`（22:48--22:52、`interrupted`）では、新しい判定（拒否あり）のまま kcheck x が終了コード0で成功し（22:51:09--22:51:54）、2本目の準備中にCtrl+Cで中断されている。成功したrunフォルダ `..._kcheck_x_S105_6jumps_scale100_20260918_225109` は、ユーザー自身が削除した（2026-09-18にユーザーが確認）。`auto_recording_session_20260918_225250.json`（22:52--22:58、`failed`）では、最初の kcheck x がLED検出失敗で約6分試行した後、終了コード2で終わり、runは削除された。**どの条件で失敗したか（従来の「閾値未満」か、新設の時刻整合・余裕か）はコンソールにしか出ておらず、試行フォルダも削除済みのため特定できていない。** OT-identの有効な記録はまだない。
- 対応: 記録コアの既定を `--pat-start-led-onset-tolerance-sec 0`、`--pat-start-led-min-peak-ratio 1` へ変更し、LEDの受理条件を2026-09-18の改修前と同じに戻した。見込みonsetは引き続き検出器へ渡し、`onset_residual_sec` と `peak_to_threshold_ratio` は常に記録する。受理された検出が「見込みから0.1 s超」または「peakが閾値の1.5倍未満」の場合は、`led_consistency_warnings()` が `[SYNC][LED][WARN]` を出し、`pat_camera_timing.json` の `pat_start_led_consistency` と `pipeline_manifest.json` の `pat_start_led.consistency`（`suspicious`、`warnings`）へ残す。runは従来どおり保存される。拒否したい場合だけ2つのオプションを明示する。検出器の関数既定（検査なし）は前項のまま。
- 失敗理由の保存: `RecordingHardwareSession.last_capture_report`（`failures`、`led_warnings`）を追加し、`run_recording()` の冒頭で初期化、LED検出失敗・記録失敗・開始失敗の理由を試行番号つきで追記する。auto入口は各runの `capture_report` としてsession JSONへ書く。失敗runが削除されても理由が残る（9/16から未実装だった改善候補）。
- 一括スクリプトに `-KeepFailedCaptures` を追加した（`--keep-failed-captures` を渡す。失敗試行のLEDイベント数の図などを残す）。
- 実データでの確認（出力は作業用フォルダのみ）: 既定の経路では、21:21と21:22の誤検出2本も当日の正しい検出2本も受理され、onset時刻は記録済みの値と一致した（改修前と同じ受理結果）。誤検出2本だけに警告が2件ずつ付き、正しい検出（残差+7.0、+7.5 ms）には付かなかった。
- 前項(2)カメラ配信停止の監視と(3)対話モードでの撮り直し確認は変更していない。
- テスト: 対象スイート計167件が成功（capture 37件: 既定値の変更に合わせて1件更新、警告つきで保存されること・失敗理由がreportに残ることの2件を追加。LED検出器6件: 残差が拒否なしでも報告されることを追記。FF 20件: 失敗理由がsession JSONへ書かれることを1件追加）。PAT・カメラは開いていない。計測成果物は変更していない。コミット・pushは行っていない。

## 2026-09-18: LED誤検出の拒否、カメラ配信停止の監視、対話モードでの撮り直し確認を実装

- ユーザー依頼（前項の改善候補1〜3をすべて実装）。実機は開いていない。計測成果物は変更していない。
- **(1) LED検出の受理条件**: `stereo_detect_pat_start_led.detect_led_sync_npz()` に `expected_onset_t_recording_sec`、`onset_tolerance_sec`、`min_peak_to_threshold_ratio` を追加した。閾値を超えた候補でも、onsetが見込みから許容幅の外、またはpeakが閾値の指定倍未満なら `RuntimeError("PAT-start LED candidate was rejected: ...")` とし、JSONへ `threshold_crossed`、`peak_to_threshold_ratio`、`onset_residual_sec`、`rejection_reasons` を残す。関数の既定値は検査なし（従来動作）で、heart delay／B1の各coreなど他の呼び出し元の挙動は変わらない。探索窓、自動閾値、onsetの定義、ROI `600,0,1280,180`、検出側（左）は変えていない。
- 記録コアは、見込みonsetを「PC時刻の見込み＋PAT送信の開始遅延（send call − 軌道時間）」として渡す。当初の既定は `--pat-start-led-onset-tolerance-sec 0.1`、`--pat-start-led-min-peak-ratio 1.5`（0／1で無効）だったが、同日夜に既定を0／1（警告のみ）へ戻した（上の項目を参照）。設定は `pat_camera_timing.json` と `pipeline_manifest.json` の `pat_start_led` に記録する。拒否された候補は従来のLED失敗と同じ経路（対話は再計測の確認、無人は自動再計測）へ進む。
- 閾値の根拠: 全記録フォルダの248本をJSONだけで集計した。正しい検出の「onset −（見込み＋送信遅延）」は中央値+7.7 ms、+4.1〜+13.5 msに集中し、例外はB3の静止圧縮run（−14〜−1 ms）、保持限界run（−30 ms）、粒子逸脱run（−55 ms）。誤検出2本は−1089 msと−1375 ms。peak÷閾値は中央値13、当日の最小は3.2。2026-08-26のPlan B1には、peakが閾値と同じ40〜44（比1.0〜1.1）でも時刻は整合している弱いLEDの検出が5本あり、新しい既定（1.5倍）では再計測になる。これは意図した変更で、今後の記録にだけ効く。
- 実データでの確認（出力は作業用フォルダのみ）: 21:21と21:22の誤検出2本は両方の理由で拒否され、当日の正しい検出3本（peak 128、147、1041）は受理され、onset時刻は記録済みの値と一致した。
- 副次的な観察（未対応）: LEDは0.5 ms間隔の点滅列として写る。現行のonsetは「探索窓内で最大のビン」から連続して閾値を超える範囲をさかのぼった時刻であり、LEDが暗いrunでは最初の点滅ではなく2発目以降を拾っている例がある（20:14のステップでは0.5 ms前にも点滅があった）。0.5 ms単位の系統誤差になり得るが、時刻の定義を変えると過去データとの整合が崩れるため、今回は変えていない。解析側でτ0（約0.8 ms）を論じる際は要注意。
- **(2) カメラ配信停止の監視**: `stereo_eventcam_record_sync.py` の各workerが、sliceを受け取るたびに時刻を、区間の状態（開始待ち／記録中／保存中）とともに共有値へ書く。親は0.5秒ごとに確認し、記録中のworkerが `--stream-stall-timeout-sec`（既定3秒、0で無効）を超えてデータを受け取っていなければ、中断を指示し、止まったworkerを終了させて、「Camera event stream stalled: the left camera delivered no data for N s ...」というエラーをそのsideのmetaとmanifest（`stream_stall_watchdog`）へ書いて終了コード1で終わる。開始待ちと保存中は監視しない。記録コアは `--camera-stall-timeout-sec`（既定3）をそのまま渡す。独立キット `eventcam_standalone/` の写しは変更していない。
- 親側の待ち時間も `compute_recorder_wait_timeout_sec()` に変更した（60秒、区間長＋20秒、post-roll＋tail＋55秒の最大）。従来の「区間長＋20秒（最低30秒）」は記録プロセス自身の診断期限より短く、22:05の失敗では診断の直後に親が打ち切っていた。
- **(3) 対話モードでの撮り直し確認**: 記録コアの `capture_retry_requested()` に判断を集約した。無人モードは従来どおり上限まで自動再計測。対話モードは、記録プロセスの失敗・タイムアウト、同期記録が始まらない失敗のいずれでも「ステレオ記録に失敗しました。…同じ軌道を再計測しますか？ (Y/n)」と尋ね、YまたはEnterで止まった記録プロセスを終了→試行フォルダ削除→開始点へ復帰→同じホログラムで撮り直す。`n`（またはEOF）のときだけ従来どおり例外となり、runは削除されsessionは止まる。理由は `pipeline_manifest.json` の `capture_retry.interactive_failures` に残る。20:13と22:05のようにsession全体とPAT接続を失うことはなくなる。
- テスト: `test_stereo_detect_pat_start_led` 6件（新規5件）、`test_stereo_sync_timing` 15件（新規7件: 停止判定、開始待ち・保存中の除外、無効化、数秒以内の打ち切り、正常終了、結果済みsideの除外、workerの状態遷移とslice毎の更新）、`test_stereo_pipeline_capture_attempts` 35件（新規5件、更新2件: 対話モードの撮り直しと辞退、LED検出への引数と設定の記録、監視オプションと親の待ち時間、不正値の拒否）。FF 19・vzr 15・HF 36・Plan B 7・auto 22・workflow 9を含む対象スイートは計164件が成功。記録プロセスとauto入口の `--help`、FF exportのdry-runも確認した。
- 既知の別件: ルートの `test_eventcam_standalone.py` は、同梱selftestの1件がルート側の同名モジュール `stereo_eventcam_record_sync` を読み込むため失敗する（キット側の写しにだけある関数を参照している）。キット内で `python -m unittest selftest` を実行すると14件成功する。今回の変更前からの問題で、未対応。
- 手順書: `STEREO_ACOUSTOOLS_3D_PIPELINE_JP.md` にLED受理条件と、記録失敗時の撮り直し・配信停止の監視を追記し、`FF_HEART_VALIDATION_MEASUREMENT_JP.md` の注意書きを更新した。コミット・pushは行っていない。

## 2026-09-18: 記録ハングは左カメラ（Master）側と判明、ステップ2本にLED誤検出、左視野の背景イベントが増加

- ユーザーが22:05のsessionの失敗時コンソール出力を提供した。3本目のOT-identでは、右カメラ（Slave、00000509）は13.5秒を正常に記録し終えた（34.5 Mイベント、capture 18.0秒）。一方、左カメラ（Master、00000508）のworkerは記録区間の開始marker（遅延7.7 ms）を出した後に完了せず、記録プロセス自身が「Camera worker timed out before completing or saving the requested capture interval」と診断した。PAT送信は正常（8.96秒）。右カメラはMasterの同期信号で最後まで動いているので、左カメラ本体は動作を続けており、止まったのは左のイベント配信か左workerの処理である。20:13のCの失敗はコンソール出力がなく、どちら側かは不明。起動時の `LIBUSB_ERROR_ACCESS` 警告は8/21にも両カメラが開けたrunで出ており、無関係の可能性が高いが、当日の成功runで出ているかは未確認。
- **LED誤検出2本（要注意）**: 21:20のsessionの最初のステップ2本、`..._kcheck_x_S105_6jumps_scale100_20260918_212109` と `..._kcheck_z_S105_6jumps_scale100_20260918_212257` は、LED peakが47（閾値47／46）で、onsetが0.295秒／0.569秒と検出された。他のステップ18本はすべて1.662--1.681秒（PC時刻の見込み＋送信遅延約1.65秒）で、この2本だけ約1.1--1.4秒早い。9/16のS170_zと同じ、ノイズ1本を採用した誤検出である。直後の21:25のrunからpeakが約1000へ上がっており、この2本の時点ではLEDが見えていなかったとみられる。2本とも `capture_complete=true`・`processing_status=pending` で確定済みで、イベント自体は正常（左3.1 M/s、右2.45 M/s）。`pat_camera_timing.json` のPAT開始時刻が誤っているので、このまま後処理するとideal比較が1秒以上ずれる。解析では除外するか、開始時刻を「見込み時刻＋送信遅延」（約1.66秒）へ置き換える必要がある。成果物は変更していない。ハート6本と残りのステップ18本のLED onsetは見込みと整合している。
- 左カメラのイベント率だけがsessionごとに増えている（同じステップ指令で、20:14 2.61 M/s → 21:01 3.07 → 21:21 3.15 → 22:07 3.32 M/s。右は2.38--2.49 M/sで一定）。20:14と22:07の左NPZをmemory-mapで集計した結果、増加分0.71 M/sの大半は、粒子（x≈620--660、y≈340）とは別の、画面右側の広い領域（x≈950--1150、y≈100--500）にあり、ブロック単位で3--5倍になっていた。LED位置とみられる点（x≈760--800、y≈0--40）も常時約6万イベント/秒を出している（20:14は約1000）。LED ROI内は0.16 → 0.29 M/s。照明の反射や迷光などの光学的な背景とみられるが未特定。記録ハングとの因果は示せていない（1回目のハングは左が2.7 M/sの時点で起きた）。図は共有済み（作業用フォルダ、リポジトリ外）。
- 対処の現状: `-Unattended` なら記録プロセスの失敗・タイムアウトを最大2回まで自動で撮り直す（前項でテスト済み）。未実装の改善候補: (1) LED検出で、見込み時刻＋送信遅延との整合と、閾値に対する十分な余裕を必須にする（誤検出は9/16の1本と合わせて3本目）。(2) 各カメラworkerに配信停止の監視を入れて数秒で失敗させる。(3) 対話モードでも記録失敗時に撮り直しを尋ね、session全体を止めない。
- PAT・カメラは開いていない。NPZは読み取りのみで、計測成果物は変更していない。コードの変更はない。コミット・pushは行っていない。

## 2026-09-18: OT-identの初回実測は3本目で記録ハング（同日2回目）、無人モードの自動再計測を確認

- ユーザーが `run_ff_heart_validation.ps1 -Designs "OTI,OTI"`（全runでpreviewとEnter）を実行した。`auto_recording_session_20260918_220540.json` は `failed`（22:05--22:11、3/8）。kcheck x（22:07）と kcheck z（22:08）は成功して `processing_status=pending` で残っている。3本目の `ffheart_OT_ident_nodelay` は、20:13のCと同じく、PAT送信後にステレオ記録プロセスが33.5秒待っても終了せず `TimeoutExpired`（終了コード1）。失敗runは通常方針で削除され、PATは停止した。OT-identの有効な記録はまだない。runフォルダ名は `feedforward_validation_005_ffheart_OT_ident_nodelay_scale100_<時刻>` で短縮されていなかった。
- 今回はPAT接続から約4分・3本目で起きたため、直前の項目に書いた「同一PAT接続の9--10本目で起きる」という整理では、この記録ハングは説明できない（9/16のLED不検出・イベント激減とは別の症状として扱う）。記録ハングは同日2回で、どちらもハートのrun（Cと OT-ident）。同日のハートは8回試行して2回ハング、ステップは20本すべて成功。偶然の可能性も残り（全28試行に一様とすると2回ともハートになる確率は約7%）、原因は未特定。
- 分かっていること: 記録プロセスは `subprocess.Popen` をパイプなしで起動しており、出力詰まりによるデッドロックではない。各カメラのworkerは `for events in iterator` でカメラ自身の時刻が終了時刻に達するまで回り、途中でイベント配信が止まった場合の監視はworker内にない。失敗時刻前後（20:24--20:26、22:09--22:11）のWindowsのSystem／Applicationイベントログに記録はなく、USB切断などは見当たらない。ハートとステップで記録プロセスの引数に違いはなく（長さだけ13.5秒と19.5秒）、PATへの送信レートも同じ10 kHz。親の待ち時間（PAT送信後33.5秒）は記録プロセス自身の診断期限（worker開始から44.5秒＋join）より先に切れるため、どちらのカメラが止まったかの診断メッセージが出る前に親が打ち切っている。コンソール出力はアプリ内端末には残っておらず、ユーザーが貼った末尾だけが手元にある。
- 回避策: `-Unattended`（`--unattended-after-first-checkpoint`）では、記録プロセスのタイムアウトも自動再計測の対象になる。止まった記録プロセスを終了させ、試行フォルダを削除し、開始点へ戻して、用意済みホログラムのまま同じrunを最大2回まで撮り直す。対話モードではこの失敗でsession全体が止まる。`test_stereo_pipeline_capture_attempts.py` に2件追加して両方の挙動を確認した（無人: 終了→再計測→成功、manifestの `capture_retry` に失敗理由が残る。対話: 例外で停止しrun削除）。実装は変更していない。OT-identの例は `-Designs "OTI,OTI" -Unattended`。無人モードでは粒子の脱落は検知されず、previewとEnterは最初の1回だけになる。
- 次に原因を追うための候補（未実装）: 親の待ち時間を記録プロセス自身の診断期限より長くして、どちらのカメラが止まったかを出力させる。worker内に「一定時間sliceが来なければ中断」の監視を入れる。失敗時にコンソール出力を残すため、実行前に `Start-Transcript` を使う。
- 検証: 無人のOT-ident並びのdry-run成功。capture 30件（新規2件）を含む対象テストは計138件が成功。PAT・カメラは開いていない。計測成果物は変更していない。コミット・pushは行っていない。

## 2026-09-18: FFハート実測3 sessionを監査（OFF／A／C各2本がそろう）、OT-identを追加

- ユーザーが `run_ff_heart_validation.ps1` で3 sessionを実測した。出力先は `stereo_acoustools_3d_records_ff_heart/`。(1) `auto_recording_session_20260918_201332.json`: 既定20 run、`failed`（20:13--20:26）。前半8 run（kcheck x/z → OFF → kcheck x/z → A → kcheck x/z）は成功、9本目のCで失敗。(2) `..._205946.json`: `-Designs "C,C"`、`complete`（20:59--21:10、8/8）。(3) `..._212012.json`: `-Designs "A,OFF"`、`complete`（21:20--21:34、8/8）。計画書の並び OFF→A→C→C→A→OFF が、前後の剛性確認ステップつきでそろった（ハート6本、ステップ18本）。全run試行1回目で成功、`capture_complete=true`、`processing_status=pending`、記録区間完全、イベント上限未到達、LED検出あり、FFのrunには `*_reference_log.csv` あり。全runでpreviewとEnter（無人モード不使用）。
- ハート6本のイベント総数は左36.7--43.6 M／右33.9--34.9 M。20:13のOFF・Aは左36.7/37.1 M・LED peak 146/205、21時台のC・A・OFFは左42.1--43.6 M・LED peak 147--1116で、session間で左のイベント数とLEDの明るさがやや異なる（9/16と同様、照明や視野の条件変化の可能性）。粒子の生存は各previewの目視のみで、NPZは開いていない。
- (1)の9本目のCは、PAT送信後にステレオ記録プロセスが33.5秒待っても終了せず `TimeoutExpired`（終了コード1）。失敗runは通常方針で削除され、PATは停止した。記録プロセスはカメラ自身の時刻が終了時刻に達するまで終わらないため、カメラのイベント配信が途中で止まった可能性が高いが、生データとコンソール出力が残っておらず確定できない。記録プロセス自身の診断期限（開始から44.5秒）は親のタイムアウトとほぼ同時で、どちらのカメラが止まったかの診断は出ていない可能性がある。空きディスク69 GB、空きメモリ16 GB、残留Pythonプロセスなし。同じC指令は(2)で2回とも正常に記録できたので、指令内容やフォルダ名の短縮が原因ではない。
- （追記: 22:05のsessionでは同じ記録ハングが3本目で起きたため、以下の「9--10本目」の整理は記録ハングには当てはまらない。上の項目を参照。）同一PAT接続の9--10本目での異常はこれで3回目（9/16の2回はLED不検出とイベント激減、今回は記録ハング）。8--9本で終えたsessionはすべて正常。原因未特定。当面は1 sessionを8 run以内にする。一括スクリプトは9 run以上で警告を出すようにした（挙動と既定の並びは変えていない）。
- 記録済みのC 2本のフォルダ名は `feedforward_validation_004_ffheart_s7_f10_C_delay_in_af5d3e6291_<時刻>`。既定の出力先ではフォルダ名の記述部が63文字までで、`ffheart_s7_f10_C_delay_inverse` は66文字になるため末尾がハッシュに置き換わる（完全なlabelは `pipeline_manifest.json` にある）。解析側の `evaluate_ff_heart.py` はフォルダ名に `_C_delay_inverse_` が含まれるかで探すため、この2本を見つけられない。解析側で `_C_delay_in` で探すか、manifestの `automation.label` を使う必要がある。runフォルダの改名はしていない。
- G:の `ff_heart/README.md` が21:43に改訂され、OptiTrap型の比較腕2本（`OT_paper_nodelay`、`OT_ident_nodelay`）が追加された。ユーザーの依頼はOT-identの計測。`heart_s7_f10_OT_ident_nodelay.npz` を `source/` へ複製し（SHA-256一致）、planの末尾へ `ffheart_OT_ident_nodelay`（export index 005、内容ハッシュ固定、「遅延なしの線形逆モデル、実機同定のk・γ」= Cから先読みを除いたもの）を追加した。最大速度720 mm/s、加速度92,506 mm/s²、\|u−r\| 0.287 mm、始終点8.6e-5 mmで既存の制限内。run名は短い形式にしたので、既定の出力先でフォルダ名に `_OT_ident_nodelay_` が残る。記録済みのOFF／A／Cの名前とexport indexは変えていない。READMEの写しも改訂版へ更新した。OT-paper（加速度98,822 mm/s²、上限の98.8%）は依頼外のため取り込んでいない。
- 作業途中で「Cの有効な記録はない」と誤認し、Cのrun名を一時的に短縮したが、(2)でCが2本記録済みと分かったため元の名前へ戻した。最終的なplanとexportでは、ステップ・OFF・A・Cの5本の `command_trajectory.npz` が、3 sessionで使ったexportとバイト単位で同一である。使用済みexportは `ff_heart_20260918/export_ff_heart_used_20260918_2013_to_2134/` に退避してある。ハート4本の指令・所望軌道・時間配列はG:のNPZとビット一致（`reference_comparison_20260918.json` を更新）。
- `acoustools_stereo_eventcam_3d_recording_auto.py` は、runフォルダ名が短縮される場合にdry-runと実機開始前で `[AUTO][WARN]` を出し、出力先が長すぎる場合はPAT接続前に終了コード2で止まるようにした。一括スクリプトへ設計名 `OTI`（`OT-IDENT`／`OT_IDENT` も可）を追加した。OT-identの例は `-Designs "OTI,OTI"`（計画書推奨の2回、ステップ込み8 run）。`FF_HEART_VALIDATION_MEASUREMENT_JP.md` に設計表・run名の節・8 run推奨を追記した。
- 検証: 再生成exportのdry-run（`OTI,OTI`、`OT-ident` 別名、既定20 runの警告）をPowerShell 5.1で確認。venvで `test_ff_heart_validation` 19件（新規2件: 公式run名のフォルダ名検査でCだけが既知の短縮例外であること、短縮時のdry-run警告）を含む計136件が成功。PAT・カメラは開いていない。計測成果物は変更していない。コミット・pushは行っていない。

## 2026-09-18: ハート7 mm・10 HzのFF指令（OFF／A／C）を実機へ送れるよう統合

- `G:\マイドライブ\Experiment\20260918\measurement_plan\ff_heart` の事前計算済み指令3本（OFF: u=r、A: 0.8 ms先読み、C: 先読み＋軸ごとの線形逆モデル。各8 s・10 kHz・80,000点、XZ面）を `ff_heart_20260918/` に統合した。README・生成器・metricsの写し（SHA-256一致）、`source/*.npz`（G:の写し、git対象外）、`ff_heart_plan.json`、`export_ff_heart/`（5 run）を置いた。exportの指令・所望軌道・時間配列はG:のNPZとビット一致（`reference_comparison_20260918.json`）。参照生成器をこのPCで再実行すると最大3e-10 mm異なるため、再生成ではなくファイル自体を取り込み、`positions_mm` の内容ハッシュをplanに固定した。
- `hf_identification_trajectory.py` に `kind=imported` を追加した。NPZの `positions_mm`／`reference_mm`／時間配列を数値そのまま取り込み、sample_hz・点数・有限値・形状・固定ハッシュを検査する。offset／速度／加速度に加え、始点終点の中心からの距離（`max_endpoint_offset_mm`）と指令と所望軌道の距離（`max_command_reference_distance_mm`）を制限し、超過は例外（自動縮小なし）。exportの `command_trajectory.npz` に `reference_mm`・`time_pat_sec`・`time_cam_expected_sec` を含め、CSVと専用preview（経路、時系列、拡大、制限比）を必ず出力する。実機読み込み時にも内容ハッシュと \|u−r\| を再検査する。planの `safety_limits` は実験単位で上書きできるようにした（1.05 mmステップと7 mmハートを同じplanに置くため。既存planは上書きを持たず挙動不変）。
- 同じexportに剛性確認ステップ `kcheck_x_S105_6jumps`／`kcheck_z_S105_6jumps`（tier C、1.05 mm、保持2.0 s、6ジャンプ、14 s、`directions=[1,-1,1]`）を入れた。計画書のサンドイッチ（各run前後に短いステップ）を `--hf-run LABEL=SCALE` の並びで実現する。
- `stereo_acoustools_3d_recording_core.py` の `PreparedTrajectory` に任意の `reference_positions` を追加した。指定時だけ `*_reference_log.csv`（所望軌道 r、ideal logと同じ形式・座標・時刻）を書き、manifestへ `reference_log` と説明を記録する。PATが再生するのは従来どおり `positions` だけで、未指定時の挙動は変わらない。`stereo_acoustools_3d_hf_common.py` は `measurement_family=feedforward_validation` を認識し、取り込み指令のmetadata検査、`shape_name=feedforward_validation`、`trajectory_source` へ `imported_command`・`command_contains_offline_feedforward`・`feedforward_design` を記録する。`delay_feedforward_applied=false` は「取得側で何も足していない」の意味のまま。
- `acoustools_stereo_eventcam_3d_recording_auto.py` は `--acknowledge-ff-validation-protocol` を新設した。FF検証runは既定で全runにpreviewとEnterを強制、`--keep-going` とstep-response以外のexportとの混在を拒否、HFの50% ackは不要。`--unattended-after-first-checkpoint` の対象を「step-response staircaseとFF検証の取り込み指令（混在可）」へ広げた（それ以外は従来どおり拒否）。session JSONの `trajectory_mode` は `feedforward_validation`、run一覧は `[FF]` 行で設計・\|u−r\|・速度・加速度を表示する。
- 一括実行 `run_ff_heart_validation.ps1` を追加した（export生成→dry-run→実機）。既定は計画書の提案どおり OFF→A→C→C→A→OFF を x／z ステップで挟む20 run・PAT再生244 s。`-Designs`、`-SandwichAxes`／`-NoSandwich`、`-CommandScale`（ハートのみ）、`-Unattended`、`-AutomaticCaptureRetries`、`-CaptureTailMarginSec`（既定5）、`-Regenerate`、`-OutputDir`（既定 `stereo_acoustools_3d_records_ff_heart`）、`-DryRunOnly`。手順は `FF_HEART_VALIDATION_MEASUREMENT_JP.md`、自動計測ガイドとREADMEに追記、`.gitignore` に `ff_heart_20260918/` のpy/md/jsonを許可した。
- 検証: 実exportのdry-run（`--hf-run` の繰り返し・scale 0.5・無人モード）成功、ackなしの実機コマンドはPAT接続前に終了コード2で停止し出力先も作られない。PowerShell 5.1で一括スクリプトの既定20 run、OFFのみ半分scale、無人モードの `-DryRunOnly` を確認。venvで `test_ff_heart_validation` 新規17件（数値不変の取り込み、ハッシュ固定、各制限の拒否、不正ソース、実験単位の制限、export内容、scale付き所望軌道、改変exportの検出、metadata不備の拒否、公式plan・ソース・export、dry-run、ack不足・keep-going・混在の拒否、全runの強制checkpoint、無人サンドイッチの順序とscale、失敗時の停止）と `test_stereo_pipeline_capture_attempts` 新規3件（所望軌道ログ）を含む、vzr 15・HF 36・Plan B 7・auto 22・workflow 9・capture 28の計134件が成功。PAT・カメラは開いていない。コミット・pushは行っていない。
- 未対応・注意: (1) 既定の20 runは約20分のsessionになる。9/16にPAT接続から約16--17分でLEDが消える異常が2回あったため、分割実行を手順書に記載した。(2) ウォームアップ／休止の待ち時間、記録末尾のLED同期、`time_cam_expected_sec` による時間軸補正つきの評価は未実装（後二者は解析側）。(3) G:の `VZR_MEASUREMENT_PLAN.md` は2026-09-17 23:37に改訂され、剛性ドループ対策（vzr runのサンドイッチ・往復順序・一定間隔）が必須とされたが、vzr側の一括スクリプトには未反映（vzrの指令列と生成器は不変）。vzr計測はまだ行われていない。

## 2026-09-17: vzr同定計測（x/y＋z同時正弦駆動）を現行ステレオautoへ統合

- `G:\マイドライブ\Experiment\20260917\measurement_plan` の計画（OptiTrapの `vzr` を取るため、水平x/yと鉛直zを共振の0.5--0.75倍・非整数比の周波数で同時駆動）を `vzr_identification_20260917/` に統合した。計画書と参照生成器 `make_vzr_trajectory.py` はSHA-256一致の写しを同フォルダに置き、`vzr_identification_plan.json` から `export_vzr_identification/`（10 run）を生成した。exportはG:の参照NPZ `positions_mm` と全サンプルで最大3.3e-16 mmの差で一致する（`reference_comparison_20260917.json`）。
- `hf_identification_trajectory.py` に2軸同時の `kind=multitone` を追加した（成分ごとに軸・振幅・固定周波数または線形チャープ・位相、既定位相0.0/0.7/2.1/3.5 rad、両端sin²ランプ）。制限（offset/速度/加速度）を超える指令は生成時に例外で止め、自動縮小しない（`safety_policy=reject_above_limits`、`safety_limit_fractions` を記録）。multitoneは `command_offset_log.csv` を常に出力し、3段の専用previewを作る。metadataの `axis` は駆動軸の連結（例 `xz`）、`axes` にリストを持つ。`reference` フィールド（計画表の段・物理Δ・SE見込み）をmetadata・trajectory_source・automationへ引き継ぐ。
- `stereo_acoustools_3d_hf_common.py` は `measurement_family=vzr_identification` を認識し（`is_vzr_metadata`）、multitoneのmetadata検査、`shape_name=vzr_identification`、parametersへ `axes`／`components` を追加する。`acoustools_stereo_eventcam_3d_recording_auto.py` は `--acknowledge-vzr-protocol` を新設し、vzr runでは全runにステレオpreviewとEnterを強制、`--keep-going`・無人モード・他exportとの混在を拒否、HFの50%ack（`--acknowledge-hf-retention-and-visibility`）は不要とした。session JSONの `trajectory_mode` は `vzr_identification`。run一覧は `[VZR]` 行で成分と制限比を表示する。
- 一括実行 `run_vzr_identification_all.ps1`（export生成→dry-run→実機、`-DryRunOnly`、`-Labels`、`-IncludeAlt`、`-CommandScale`、`-Regenerate`、`-OutputDir` 既定 `stereo_acoustools_3d_records_vzr`）を追加した。既定の順序はL1x→L1z→L1→L2→L3→L4→Y1y→Y1→Y2の9 runで、ALT（x 0.7 mm @ 50 Hz）は任意。手順は `VZR_IDENTIFICATION_MEASUREMENT_JP.md`、`STEREO_ACOUSTOOLS_3D_AUTO_JP.md` とREADMEに追記、`.gitignore` に `vzr_identification_20260917/` のpy/md/jsonを許可（exportは除外）。
- 検証: 実exportのdry-run／preview-only成功、ackなしの実機コマンドはPAT接続前に終了コード2で停止（出力先も作られない）。PowerShell 5.1で一括スクリプトの `-DryRunOnly`、`-Labels`＋`-IncludeAlt`＋`-CommandScale 0.5`、不正label拒否を確認。venvで `test_vzr_identification`（新規15件: 参照生成器との1e-12 mm一致、制限超過の例外、成分検査、チャープ成分、公式planの順序・制限、export読み込みと2軸位置、dry-run、ack・keep-going・無人・混在の拒否、強制checkpoint、label部分選択の順序）と既存のHF 36件・Plan B 7件・auto 22件・workflow 9件・capture 25件の計114件が成功。PAT・カメラは開いていない。vzrの同定コード（`real_vzr_fit.py`）はこのリポジトリに含まれない。コミット・pushは行っていない。

## 2026-09-16: S025--S070を再計測、単一振幅系列18 runが完全にそろう

- `auto_recording_session_20260916_171318.json` は `complete`（17:13--17:25、無人モード、`--capture-tail-margin-sec 5`）。S025/S040/S070 × X/Y/Zの9 runが試行1回目で成功し、自動再計測は0回。1 runあたり約70秒だった。
- 9 runとも、同期検証、左右記録区間31.5秒完全、イベント上限未到達、PAT送信errorなし。LED onsetは3.072--3.107秒で、各runの送信遅延+約0.012秒と整合する（peak 1284--2169、閾値44）。最後の保持2.0秒まで記録区間に入っている。左右イベントは左75.1--76.4 M、右69.2--70.1 Mで安定。ホログラム計算はrunごとに位置3種類・0.14--0.15秒（最初のrunはPAT出力前に事前計算）。
- ユーザーが旧S025--S070の9 runと無効な旧S170_z（`..._163718`）を削除済み。`stereo_acoustools_3d_records_step_response_2s/` のrunディレクトリは18個で、各条件1本ずつ。いずれも最後の保持まで記録済み、`processing_status=pending`、LED onsetは送信遅延と整合している。構成は、S025--S070が17:14--17:24の9本、S105--S170_yが16:23--16:35の8本、S170_zが17:03の1本。
- 16:22の8 runと17時台の10 runでは、LED peak（約460--580と約1280--2170）と左イベント総数（77--78 Mと75--76 M）がやや異なる。17時台の前にLEDや視野の条件が変わった可能性がある。
- 同フォルダに残る記録のない、または削除済みrunだけを指すsession JSON（153140、153629、154528、154734、170035、170159）は削除候補。162219は有効な8 runを含むが、削除済みの旧S170_zへの参照も残る。この環境からは削除できないため、ユーザーの手作業待ち。粒子生存の確認は各sessionの最初のrunの目視のみ。PAT・カメラは開いていない。

## 2026-09-16: S170_zを再計測、単一振幅系列の18条件がそろう

- 対話モード（`--label S170_z_single_amplitude_2s_hold --capture-tail-margin-sec 5`）で再計測した。17:00と17:01のsessionは、最初のrunが終了コード1・エラー文字列なし・operator checkpointありで終わり、runディレクトリは作られていない（プレビュー中止の経路）。17:02のsession `auto_recording_session_20260916_170259.json` は `complete`。
- 新しい `step_response_identification_002_S170_z_sing_21ba26d879_20260916_170345` は試行1回目で成功した。同期検証、左右記録区間31.5秒完全、イベント上限未到達、PAT送信errorなし。LED onsetは3.104秒で、送信遅延3.090秒と整合する（peak 1698、閾値44、集計図で明瞭な立ち上がり）。最後の保持2.0秒まで記録区間に入っている。左右イベントは74.8 M/69.8 Mで、同条件の正常run（左77.1--78.4 M、右70.7--72.2 M）に近い。
- ホログラム使い回しを実機で確認した。`distinct_position_count=3`、`hologram_solve_seconds=0.14`。プレビュー画像保存からideal log保存までの準備区間は、変更前の約60秒から約26秒になった。
- データ現況：18条件すべてに有効runが1本ずつある。S025--S070の9 runは最後の保持が0.93--1.42秒で途切れている。無効な旧S170_z（`..._20260916_163718`、LED誤検出）は同じフォルダに `processing_status=pending` のまま残っており、一括後処理やスパコン転送の前に除外が必要（移動はユーザー確認待ち）。粒子の生存確認は各sessionの最初のrunの目視のみ。PAT・カメラは開いていない。

## 2026-09-16: 同一位置のホログラムを使い回して準備時間を短縮

- ユーザー要望（ステップ応答のホログラム計算が遅い）を受け、`stereo_acoustools_3d_recording_core.py` に `compute_scaled_holograms_for_positions()` を追加した。再生用ホログラムは位置の種類ごとに1回だけ `compute_holograms_for_positions()`（KD solver）を呼び、同じ位置のフレームでは同じテンソルを共有する。位置は初出順・元の値のまま渡し、完全一致で判定する（-0.0と0.0は区別）。フレームごとの値は従来と同じで、指令は粗くしていない。更新レート10 kHz、送信データ作成（`prepare_message_from_holograms`）、PAT送信経路は変更していない（ユーザーはホログラム計算だけの変更を選択）。
- `run_recording()` の通常経路と `precompute_hologram_playback()`（完全静止の圧縮経路は従来どおり）に適用した。`pipeline_manifest.json` の `hologram_playback` へ `distinct_position_count` と `hologram_solve_seconds` を追加し、`PrecomputedHologramPlayback` に `distinct_position_count` を追加した。下流の処理はテンソルを読むか複製してから変更するだけなので、共有しても安全。
- 単一振幅系列の実export 18 runは各260,000フレーム・位置3種類（中心・±A）。KD solverの呼び出しは1 runあたり260,000回から3回になる（従来の実測は約33秒）。位置の重複判定は約0.18秒で、全フレームが従来の個別計算と同じ位置のホログラムを受け取ること、solverへ渡る座標がPythonのfloatであることを確認した。ランダム軌道のように位置がすべて異なる場合は、従来どおりフレームごとに計算する。
- `test_stereo_pipeline_capture_attempts.py` に5件追加（共有と値の一致、-0.0の区別と入力値の受け渡し、位置がすべて異なる場合の従来動作、staircaseの事前計算、`run_recording` の再生計算とmanifest記録）。torch/acoustools/metavisionをスタブ化した環境で、capture 25件・HF/step 36件・auto/workflow 31件の計92件が成功した。重複判定を無効にする変異試験では新規テストのうち4件が失敗することを確認した。`STEP_RESPONSE_SINGLE_AMPLITUDE_JP.md` の準備時間とメモリの説明を更新した。PAT・カメラは開いていない。コミット・pushは行っていない。

## 2026-09-16: 単一振幅系列の再開session（S105--S170）監査、S170_zは無効

- `auto_recording_session_20260916_162219.json` は `status=complete`（16:22--16:40）。1.05/1.40/1.70 mmのexportを `--capture-tail-margin-sec 5` 付きで無人実行し、9 runのディレクトリが作成された。記録は31.5秒となり、全runで最後の中心保持2.0秒まで記録区間に入った。
- S105 X/Y/Z、S140 X/Y/Z、S170 X/Yの8 runは試行1回目で成功。同期検証、左右記録区間完全、イベント上限未到達、PAT送信errorなし、LED onset 3.08--3.63秒（peak 458--579、送信遅延+約0.01秒で整合）、左右イベント総数は左77.1--78.4 M、右70.7--72.2 Mで安定。
- **S170_z（`step_response_identification_002_S170_z_sing_21ba26d879_20260916_163718`）は無効。** 試行1はLED peak 39で失敗。試行2は閾値43に対しpeak 44のノイズ1本を t=0.128 秒のonsetとして誤採用し、「成功」として `processing_status=pending` で確定した。送信遅延3.08秒から期待されるonset約3.09秒とは合わず、LED集計図にも点灯はない。左右イベントは27.3 M/33.3 Mで、正常runの約35%/46%。削除・移動はしていない（ユーザー確認待ち）。
- S170_zの左NPZ（382 MB）だけをクラウドで確認した。記録全体で粒子らしき像はなく、左カメラ画面の x<約780・y>約100 の範囲は、辺が斜めの暗い四角形になっていた。この範囲の画素の98%は31.5秒間イベント0で、`mask_rois` は空（ソフトウェアのマスクではない）。正常runのNPZは800 MB超で転送できず、正常時の見え方とは比較していない。
- 両sessionとも、PAT接続から約16--17分後（9--10本目）に、LEDが消え、左右イベント総数も大きく減るという同じ異常が起きた。原因は未特定（PAT出力停止、視野の遮蔽などが候補）。
- 改善候補：LED検出で、送信遅延から予想されるonsetとの整合と閾値に対する十分な余裕を必須にし、今回のような誤検出を拒否する。無人モードで、イベント総数がsession最初のrunより大きく減ったら停止する。自動再計測の失敗理由をsession JSONへ記録する。いずれも未実装。
- データ現況：記録として有効なのは17/18 runで、S025--S070の9 runは最後の保持が0.93--1.42秒で途切れている。S170_zは要再計測。粒子の生存確認は各sessionの最初のrunの目視のみ。PAT・カメラは開いていない。

## 2026-09-16: 単一振幅2.0秒保持系列の無人実測（9/18 runで停止）

- `stereo_acoustools_3d_records_step_response_2s/` を軽量監査した（manifest・timing・LED・recording metaのみ。イベントNPZは開いていない）。session `auto_recording_session_20260916_154734.json` は `status=failed`。15:31/15:36/15:45の3 sessionは最初のrunで終了コード130（Ctrl+C）となり、runディレクトリは作られていない。
- 成功9 run：S025/S040/S070 × X/Y/Z。全runが試行1回目で成功し自動再計測なし、`capture_complete=true`、`processing_status=pending`、左Master/右Slave同期検証済み、左右とも記録区間完全・イベント上限未到達、左LED ROI `600,0,1280,180` 検出（peak 328--599、閾値42--43）、PAT送信errorなし。左右イベント総数は左67.0--69.7 M、右59.0--63.9 Mで9本とも安定。最初のrunだけoperator checkpoint付き、ホログラム事前計算33.0秒。1 runあたり約100秒で進行した。
- 10本目 `S105_x_single_amplitude_2s_hold` はLED検出が3試行とも失敗し、自動再計測上限（2回）到達で終了コード2、16:07にsession停止・PAT停止。失敗runは削除済み。ユーザーが貼ったコンソールの試行3では、LED peak_count=39（閾値42）、左23.5 M・右28.7 Mイベントで、成功runの約1/3と約1/2しかない。PAT送信呼び出しは29.07秒（遅延3.07秒）で正常、LED探索窓±3.27秒は想定onset約3.08秒を含む。LEDと左右両カメラのイベントが同時に大きく減ったため、閾値やROIの問題ではなく、PAT出力（LEDを含む）が実際には出ていなかったか場面自体が変わった可能性が高い。原因は未特定。失敗理由はsession JSONには残らない（改善候補）。S105/S140/S170の9 runは未計測。
- PAT送信開始遅延が3.07--3.56秒（260,000フレーム）あり、記録28.5秒（tail margin 2.0秒）を超えて軌道末尾0.58--1.07秒が記録外。12ジャンプは全て記録内だが、最後の中心復帰後の保持は2.0秒中0.93--1.42秒のみ。残りの計測では `--capture-tail-margin-sec` を約5秒へ増やす必要がある。
- 成功9 runの粒子生存・両眼可視は未確認（NPZが1本0.83--0.98 GBでクラウドへ転送できないため）。PAT・カメラは開いていない。

## 2026-09-16: 単一振幅ステップ応答18 runの無人連続計測に対応

- ユーザー要望（run間確認は最初の1回だけ、失敗時は自動再計測→だめなら停止）により、`acoustools_stereo_eventcam_3d_recording_auto.py` の `--hf-export-dir` を複数指定可能にした。指定ディレクトリ順・manifest順に1回のPAT/OpenMPD接続で記録する。run名のexport間重複、同一ディレクトリの重複指定、複数export時の `--hf-run`／`--start-index`／`--limit`／Plan B selector はPAT接続前に拒否する。各runの `automation.export_manifest` は所属exportのmanifestを指す。session JSONへ `source_export_manifests` を追加し、複数時の `source_export_manifest` は `multiple; see source_export_manifests`。
- `--unattended-after-first-checkpoint` を追加。全選択runがstep-response staircaseの場合だけ有効で、Tier H、`--confirm-each-run`、`--preview-each-run`、`--keep-going` とは併用不可。`--acknowledge-step-response-risk` は引き続き必須。最初のrunのホログラムをPAT出力前に計算し、最初のrunだけステレオpreviewとEnter確認を行う。2 run目以降はpreview・Enterなし。各run後にホログラム参照を解放してから次runを計算する。
- `stereo_acoustools_3d_recording_core.run_recording()` に `automatic_capture_retries` を追加。無人モードでは同期開始失敗・記録プロセス異常終了/タイムアウト・PAT送信失敗・LED検出失敗を、用意済みホログラムのまま最大N回（既定2、`--automatic-capture-retries` で0--10）自動再計測する。失敗試行は記録プロセスを停止して試行ディレクトリを削除し、2秒待ってから開始点へ戻して再武装する。上限到達時はLED失敗がexit 2（run削除）、その他は従来どおり例外となり、セッションは停止してPATを止める。`None`（従来の対話モード）の挙動は変更していない。`pipeline_manifest.json` へ `capture_retry`、automationへ `operator_checkpoint_before_capture` 等を記録する。
- 粒子脱落の自動検知は実装していない。無人区間で脱落しても残りrunは記録が続く。
- 一括実行用 `run_step_response_single_amplitude_all.ps1` を追加（未生成exportの生成→dry-run→実機、`-DryRunOnly`、`-Amplitudes`、`-AutomaticCaptureRetries`、`-Regenerate`、`-OutputDir`。既定出力先 `stereo_acoustools_3d_records_step_response_2s`）。`STEP_RESPONSE_SINGLE_AMPLITUDE_JP.md` と `STEREO_ACOUSTOOLS_3D_AUTO_JP.md` を更新。
- torch/acoustools/metavisionをスタブ化した環境で、HF/stepテスト36件（新規6件）、auto/workflow入口テスト31件（変更前と同じく全件成功）、capture attemptテスト20件（新規8件：LED・記録・同期開始失敗の自動再計測、上限到達時の停止と削除、対話モード不変、不正値拒否）が成功。変異試験で記録失敗の再試行を無効化すると新規テストが失敗することを確認。PowerShell 7.4でスクリプトのdry-run・偽ハードウェア実行・終了コード伝播・不正振幅拒否を確認。実exportの18 runをモックハードウェアで通し、順序、最初の1本だけpreview/Enter、全runの再試行上限2、PAT接続1回を確認。PAT・カメラは開いていない。コミット・pushは行っていない。

## 2026-09-16: ステップ応答の前Tier生存確認フラグを廃止

- ユーザー要望により、`acoustools_stereo_eventcam_3d_recording_auto.py` から `--acknowledge-step-response-tier-a-survived`〜`--acknowledge-step-response-tier-g-survived` の7フラグと、Tier B〜Hでそれらを要求していた検査を削除した。旧フラグを指定するとargparseエラーになる。
- 残した安全ゲート: staircase共通の `--acknowledge-step-response-risk`、Tier H専用の `--acknowledge-step-response-escape-boundary-probe` と1 session 1 run制限、`--keep-going` 禁止、各run前のステレオプレビュー・Enter確認の強制、生成側の `max_step_mm`／脱出境界検査。前Tierの生存確認は運用（各run前の確認）で行う。
- 単一振幅2.0秒保持系列（`step_response_single_amplitude_*_plan.json`）は全振幅で `--acknowledge-step-response-risk` だけで実行できる。
- `test_hf_identification_recording.py` の生存ack前提テストを、B〜Gがrisk ackだけで進むこと・Hは引き続き専用ackと1 run制限を要すること・旧フラグが拒否されることの確認に置き換えた。`STEP_RESPONSE_MEASUREMENT_JP.md`、`STEP_RESPONSE_ESCAPE_BOUNDARY_JP.md`、`STEP_RESPONSE_SINGLE_AMPLITUDE_JP.md` のコマンドと説明を更新。過去の記録項目は当時の内容のまま残している。
- torch/acoustools/metavisionをスタブ化した環境で対象テスト30件成功。0.70/1.05/1.40/1.70 mm の実exportを生成し、ハードウェアをモックした状態でrisk ackなしは開始前に停止、risk ackのみで開始・Enter確認が有効になることを確認した。PAT・カメラは開いていない。コミット・pushは行っていない。

## 2026-09-16: AcousTools不要の独立イベントカメラ計測キットを作成

- `eventcam_standalone/` へカメラ単独の校正・同期撮影・粒子抽出・3D復元に必要な既存Python 12本のコピー、工程を連結する `eventcam_workflow.py`、設定、環境確認・有効化、実機不要の `selftest.py`、日本語README・OpenEB導入指示書・検証記録を格納した。プロジェクト固有コードは同ディレクトリだけで完結し、AcousTools/OpenMPD/PyTorchのimport・接続は行わない。
- 共通入口は `all`、各校正工程、`record`、`process`、`record-process`、副作用のない `--dry-run` に対応。左00000508 Master／右00000509 Slave、7.12 mm・9×6内部コーナーを維持する。新規校正はキット内部のセッションに生成し、runごとに校正コピーとハッシュ・設定を保存する。既存の校正・計測成果物、元プロジェクトのLED／ROI／校正パスは変更していない。新キットはPAT開始LEDを使用しない独立計測であり、PAT座標変換・理想軌道比較は含めない。
- ソースの校正パスを必須指定に変更し、SDK探索先をキット内または明示環境変数へ限定。親ディレクトリへ依存せず、移設後はrun内の校正で解析する。既存校正・同名runへの上書きと、異なる解析条件での `--resume` を拒否する。`.gitignore` のコード許可リストとREADMEを更新。コミット・pushは行っていない。
- 対象テスト9件成功。切り離したコピーで禁止import検査、全CLI help／dry-run、人工左右イベントからの2D抽出・既知深度600 mmの3D復元・描画・再開を確認。ROOTの `test_eventcam_standalone.py` は同梱selftestの起動用。元12本はコピー元ハッシュと一致。PAT、カメラ、既存計測データの重い処理、SDKインストールは実行していない。実機取得・クリーン環境ビルドは未検証。

## 2026-09-16: イベントカメラ計測・粒子抽出の手順を集約

- `EVENTCAM_MEASUREMENT_EXTRACTION_GUIDE_JP.md` に、計測／後処理の入口と依存スクリプト、固定カメラ・LED・校正設定、実行例、出力、スパコン移設時の必要データとパス調整をまとめた。READMEからリンクした。
- 現行ソースの依存関係・引数と、後処理／単眼抽出入口の `--help` を確認した。実装・計測状態は変更していない。PAT、カメラ、計測、重い粒子抽出、データ転送は実行せず、既存成果物も変更していない。

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

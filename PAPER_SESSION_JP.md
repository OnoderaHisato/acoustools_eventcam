# フルペーパー用の計測 S1〜S4（2026-10-14 計画、計測 PC の手順）

正本は `G:\マイドライブ\Experiment\20261014\PAPER_SESSION_PLAN_20261014.md`（10/15 の追記つきの写しが
`paper_20261014/PAPER_SESSION_PLAN_20261014.md`）と、撮る順の一覧 `paper_20261014/runlist/S1〜S4_runlist.csv`。
論文のデータを 0 から撮り直す。補償の run は無い（S5 は別途）。

## 取り込んだもの（`paper_20261014/`）

- `paper_plan.json`: 一覧に出てくる 296 種類の設計（内容のハッシュで固定、上限は規模拡大の値 35 mm / 3500 mm/s /
  350k mm/s² / \|u−r\| 4.5 mm / 端点 0.01 mm）。
  - G: から写した新しい指令 201 本: `ff_paper` 159、`w_scan_yoffset` 24、`kgrid_paper` 6、`ff_old_extra`
    （ずらしたハートの OFF 6 本、design_dir は `ff_heart_15V`）、`w_scan_dense` 6（`Experiment/20261012/`、48 s）。
  - 旧データの設計 95 本は `cooling_20261006` / `session2_20261011` / `extra3_20261012` の plan から同じ run 名で
    写した（`copied_from` に元を残す。一覧の `design_dir` 列で元の plan を選ぶ）。
- `export_paper/`: 296 本の export。新しい 201 本は G: の npz とビット一致、再利用の 95 本は元の export と
  バイト一致（`reference_comparison_20261015.json`）。
- run 名は最長 27 文字（`random_rms0.5_5-60Hz_p0_OFF`）で、`stereo_acoustools_3d_records_V15c` の下で短縮されない。
- 新しい指令の最大値: オフセット 28.11 mm（`rotline_r23_d15_yp6_f10_OFF`）、速度 1631 mm/s（`liss32_w36_f4_OFF`）、
  加速度 239,066 mm/s²（`heart_s7_f16_OFF`）。`rotline_*` 16 本は 1 標本の変化が最大でも 3.2 mm よりずっと小さく、
  imported の検査（staircase のジャンプ上限は掛からない）を通る。

## 実行（`run_paper_20261014.ps1`）

```powershell
# 実機を開かない確認（dry-run）
powershell -ExecutionPolicy Bypass -File .\run_paper_20261014.ps1 -Session S1,S2 -DryRunOnly

# 1 日目: S1 → 続けて S2（暖機 40 分は最初だけ。S2 の頭の kcheck/hold は S1 の終わりで代える）
powershell -ExecutionPolicy Bypass -File .\run_paper_20261014.ps1 -Session S1,S2 -PsuUsb

# 2 日目: S3 → 続けて S4（粒子 0 → 1 → 2 → 3。交換のところで止まって Enter を待つ）
powershell -ExecutionPolicy Bypass -File .\run_paper_20261014.ps1 -Session S3,S4 -PsuUsb

# 粒子が落ちたら Ctrl+C → 粒子を替える → 同じ -Session の番号で再開（番号は起動時の一覧に出る）
powershell -ExecutionPolicy Bypass -File .\run_paper_20261014.ps1 -Session S1,S2 -WarmupMin 0 -StartAt 57 -PsuUsb
```

- 順番は csv のとおり。`-Session` は 1 つか 2 つ（`S1,S2`、`S3,S4`）。2 つ目のセッションの頭の kcheck/hold は
  省く（`-KeepHead` で残す）。S4 の頭は粒子の交換なので省かない。
- 本数と見積もり（1 本 = 記録＋65 s、交換 3 分、暖機は別）: S1 115 本 145 分、S1+S2 257 本 320 分（暖機込み 6 時間）、
  S3+S4 230 本・交換 3 回 313 分（暖機込み 5 時間 53 分）。解析側の `summary.md`（S1 145、S2 180、S3 199、S4 114 分）と合う。
- 確認（プレビューと Enter）: 最初の run と粒子の交換のあとだけ（10-16 から。粒子は各 run のあとに自動で確かめるため、`cardioid_a23` などの前の確認はやめた。必要なら `-CheckpointBefore "cardioid_a23_f10_OFF,rotline_*"`）。
- 温度の記録は 10 分ごと。Tempo Disc と DS18B20 の値がそろっていれば聞かずに自動で記録し、欠けているときだけ画面で聞く。セッションの終わりに 1 分ごとの表 `env_timeline_<時刻>.csv`（室温・湿度・気圧・上下 PAT・電源・その分の run）を書く。DS18B20 は COM12 の Arduino（`pat_temp_sensors.json`: top_pat・bottom_pat、10-15 から自動）。この Arduino は開始 LED と照明の点滅も受け持つので、**計測の前にモニター画面 `pat_temp_app.py` を閉じる**（COM12 を握るため）。電源・環境・PAT 板の温度のロガーは既定で起動
  （`-NoEnvSensor`、`-NoPatTempSensor`）。電流の見張り 3 %。
- **記録や開始 LED の検出に失敗したとき**: 聞かずに自動で撮り直す（`-AutomaticCaptureRetries`、既定 2 回）。それでもだめなら通知（`led_failure` / `capture_failure`、メールと見張りセッション）して、画面の「同じ軌道を再計測しますか？ (Y/n)」で待つ。`-Unattended` なら聞かずにセッションを止める。
- **粒子が落ちたかを各 run のあとに自動で確かめる**（`-ParticleCheck pause`、既定）。落ちていれば通知が来て、次の run の前で止まって待つ。置き直して Enter → 同じ run を 1 回撮り直す（2 回落ちたら次へ）。詳しくは `PARTICLE_PRESENCE_JP.md`。

## 終わりの粒子の保持（新規、既定で有効）

セッションの終わり（complete、電流の見張り、失敗、Ctrl+C）に PAT を切らず、**粒子を中心で保持したまま Enter を待つ**。
auto 入口の既定（ユーザーの指示 10-15）なので、ほかの一括スクリプト（session2・extra3・冷却など）も同じ動きになる。
切るのは `--no-hold-particle-until-enter`（paper の runner では `-NoHoldAtEnd`）。30 s ごとにビープ、5 分ごとに画面に表示。
`--hold-particle-max-min`（既定 180 分）を過ぎると自動で切る。保持中の Ctrl+C はすぐ切る。
session JSON の `particle_hold` に始め・終わり・終わり方（`enter` / `timeout` / `interrupt` / `no_console`）を残す。
粒子を回収してから Enter を押す。

## 通知（新規）

計測スクリプトが自分で知らせるもの: 終わり（`session_complete` / `session_stopped`）、粒子の交換の前（`particle_change`）、粒子が落ちた（`particle_lost`）、
自動の撮り直しでもだめで Y/n を聞く前（`led_failure` / `capture_failure`）、プレビューの確認待ち（`checkpoint`）、操作の待ち（`operator_action`）、
電源の出力 OFF・電流の急落（`supply_event`）、温度・LED の Arduino の再起動（`pat_temp_interrupted`）、開始 LED の時刻のずれ（`led_warning`）。
一括スクリプトは外の見張り `live_session_watch.py`（`--stall-only`）も自動で起動し、run が 5 分終わらない（`capture_waiting`）・
8 分進まない（`session_stall`）ときだけ知らせる（固まった場合用。`-NoSessionWatch` で起動しない）。

`notify_config.json`（git には入らない。見本 `notify_config.example.json`）があれば、次のときに知らせる:
セッションの終わり（complete / stopped。PAT が保持中かどうかも書く）、粒子の交換の前。

- `email`: SMTP（STARTTLS か SSL）。**パスワードは設定ファイルに書かない**。`password_env` の環境変数か
  `password_file` のファイル（リポジトリの外）から読む。Gmail はアプリ パスワード。
- `event_dir`: `notify_events.jsonl` に 1 行ずつ追記し、`latest_event.json` を書き換える。会話中の Claude が
  このファイルを見張っていれば（Monitor）、アプリの通知で知らせられる。**Claude の見張りは会話が続いている間だけ**
  なので、長い計測は email を主にし、Claude は補助。
- `beep`: PC のスピーカー。

通知は `--notify-config` を明示したときだけ出る（一括スクリプトは `notify_config.json` があれば自動で渡す。テストや手で起動した auto からは送らない）。送信に失敗しても計測は止まらない（`[NOTIFY][WARN]`）。`--no-notify` で止める。
試し送り: `.env\Scripts\python.exe .\session_notify.py --test`（email・イベントファイル・ビープを 1 回ずつ）。

Gmail のアプリ パスワード（ユーザーが行う。Claude はパスワードを扱わない）:
1. Google アカウントで 2 段階認証プロセスを有効にする（https://myaccount.google.com/security）。
2. https://myaccount.google.com/apppasswords で名前（例 `PAT notify`）を付けて作成し、16 文字を控える。
   大学などの Workspace のアカウントでは管理者が無効にしていることがある（その場合は個人の Gmail）。
3. PowerShell で、ユーザーの環境変数に入れる（新しく開いた窓から有効）:
   `[Environment]::SetEnvironmentVariable("PAT_NOTIFY_SMTP_PASSWORD", "<16文字、空白なし>", "User")`
4. `notify_config.json` の `username`・`from`・`to` に Gmail のアドレスを書き、`email.enabled` を `true` にする。
5. 新しい PowerShell の窓で `session_notify.py --test` を実行し、メールが届くか確かめる。

Claude 経由: 計測を始めるときに会話で知らせてもらえれば、Claude が `notify_events/latest_event.json` を見張り
（Monitor）、イベントが来たらプッシュ通知（Remote Control をつないでいればスマホにも）を送る。会話が続いている間だけ。

## 粒子の記録（計測中の画面で入力、2026-10-16）

`run_paper_20261014.ps1` は既定で `--particle-prompts` を付ける（`-NoParticlePrompts` で無効）。画面で聞くのは次の 3 回:

| いつ | 入力 | 記録 |
|---|---|---|
| 始め（PAT が出た直後、粒子を置く前） | `番号 d1 d2 d3 [メモ]`（番号は一覧から分かるので省略可。例 `1.5 1.6 1.5`） | 直径（使う前）、置いた時刻 |
| 粒子の交換（新しい粒子を確かめた Enter のあと） | ① 外した粒子の直径（使った後）`d1 d2 d3 [メモ]` ② 新しい粒子 `番号 d1 d2 d3 [メモ]` | 外した時刻、直径（後）、新しい粒子の直径（前） |
| 終わり（保持中に回収して Enter のあと） | 回収した粒子の直径（後） | 外した時刻、直径（後） |

- 空の Enter で飛ばせる。10 分（`--particle-prompt-timeout-sec`）だれも打たなければ飛ばして計測を続ける。
- 番号の代わりに `0'` などと打てば替えた粒子として残る。小数点は `,` でもよい。
- `-Calipers "型番, 0.01 mm" -MeasuredBy 小野寺 -ParticleSource "袋 A, ロット"` は全粒子に付く。
- session JSON の `particles`（粒子ごとに番号・置いた/外した時刻・その粒子で撮った run・直径の前後・平均と最大−最小）と、各 run の `particle`。
- 落ちた run・保管のしかた・写真などは、あとから `particle_record.py` で足す（下）。画面で入れた内容は消えず、足した分だけ加わる。

## 粒子の記録をあとから足す・直す（`particle_record.py`、計画 §4）

auto は run ごとに session JSON を書き直すので、計測中は横の `particles_<stamp>.json` に書き、終わってから
（`--merge` か `--removed-at` で）session JSON の `particles` に入れる。

```powershell
# 置く前（session JSON がまだ無い）: 出力先に particles_pending.json を作る → 次の session が引き取る
.\venv\Scripts\python.exe .\particle_record.py --output-dir .\stereo_acoustools_3d_records_V15c --particle 0 `
  --diameters 1.5,1.6,1.5 --measured-when before --calipers "型番, 0.01 mm" --measured-by 小野寺 `
  --source "袋 A, ロット" --appearance "欠けなし" --placed-at now

# 計測後: 外した時刻と保管のしかたを足して session JSON に入れる
.\venv\Scripts\python.exe .\particle_record.py --session .\stereo_acoustools_3d_records_V15c\auto_recording_session_<stamp>.json `
  --particle 0 --removed-at now --storage "シャーレ、ふた付き" --merge

# 落ちた run
.\venv\Scripts\python.exe .\particle_record.py --session ... --particle 0 --dropped-run heart_s7_f10_OFF
```

粒子番号は 0, 1, 2, 3（落ちて替えたら `0'`）。直径は向きを変えて 3 回（平均と最大−最小を自動で出す）。
どの run がどの粒子かは、`placed_at` / `removed_at` と session JSON の `particle_changes` の時刻で決まる。

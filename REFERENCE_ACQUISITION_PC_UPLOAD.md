# 計測 PC から Miyabi へ直接送る手順（2026-09-21）

これまでは「計測 PC → SSD → Mac → Miyabi」だった。SSD と Mac を外し、計測 PC から Miyabi へ直接送って 3D 化ジョブまで投入する。
解析側（Mac）は、Miyabi で 3D 化が済んだ run の比較結果だけを取り込む（1 run 約 7 MB）。

## 1. 一度だけの準備（計測 PC）

1. **OpenSSH クライアント**が入っていること（Windows 10 / 11 は標準。PowerShell で `ssh -V`）。
2. **Miyabi 用の鍵**を計測 PC に置く。Mac で使っている鍵を写すのではなく、計測 PC で新しく作って Miyabi のポータルに登録するのが安全:
   `ssh-keygen -t ed25519 -f %USERPROFILE%\.ssh\id_ed25519_miyabi` → 公開鍵（.pub）を Miyabi の利用者ポータルで登録。
3. `%USERPROFILE%\.ssh\config` に:
   ```
   Host miyabi-c.jcahpc.jp
     User x10733
     IdentityFile ~/.ssh/id_ed25519_miyabi
     ServerAliveInterval 30
   ```
4. 一度 `ssh miyabi-c.jcahpc.jp` で入り、2 段階認証まで通ることを確認する。
   Windows の OpenSSH は接続の共有（ControlMaster）が使えないので、**ssh を起動するたびに 2 段階認証が要る**。
   `miyabi_upload.py` は 1 バッチ（最大 9 run）を 1 回の ssh で済ませるので、認証は 9 run につき 1 回。
5. このフォルダの `miyabi_upload.py` を計測 PC に置く（Python 3.8 以降、標準ライブラリのみ）。

## 2. 毎回の手順

```
cd C:\Users\digit\Documents\scripts\python\eventcam_control
python miyabi_upload.py stereo_acoustools_3d_records_ff_heart --since 20260921_2200 --dry-run   # 対象の確認
python miyabi_upload.py stereo_acoustools_3d_records_ff_heart --since 20260921_2200             # 送信＋ジョブ投入
```

- `--since` はその時刻以降の run、`--match 文字列` は名前で絞る。両方指定も可。
- feedforward_validation（解析で先に要るもの）を先に、あとは時刻順に送る。9 run ごとに 1 ジョブ。
- 送りながら gzip で圧縮する（イベントデータは約 1/3 になる）。研究室の有線なら 1 run（1.5 GB）1 分前後の見込み。
- 最後に run ごとの合計バイト数を Miyabi 側と照合して `OK` / `MISMATCH` を表示する。`MISMATCH` の run だけ `--match` で送り直す。
- ジョブは `QSUB 34xxxxx.opbs` と表示される。Miyabi の同時実行は 2 ジョブまでで、超えた分は順番待ちになる（そのまま待てばよい）。
- 3D 化は 1 ジョブ約 50–60 分。終わったら Claude（Mac 側）に「records フォルダ名と時刻」を伝えれば、比較・取り込み・評価を行う。

## 3. Miyabi 側の決まりごと（スクリプトが自動でやっていること）

| 項目 | 内容 | 理由 |
|---|---|---|
| 置き場所 | `/work/xg25g006/x10733/eventcam/stereo_3d/<records フォルダ名>/<run>` | 3D 化の PBS とスクリプトがここにある |
| グループ | 展開後に `chgrp -R xg25g006`、フォルダに `chmod g+s` | 個人グループ x10733 の /work は 50 GB 上限。超えると書き込みが途中で失敗する（転送が「途中で切れる」ように見える） |
| 一覧 | `list_<日時><a,b,…>.txt`（1 行 1 run、`<records>/<run>`） | PBS が読む |
| ジョブ | `qsub -q regular-c -N up_<…> -v LIST=<一覧>,NPAR=<本数> run_post_and_compare_list.pbs` | マニフェストのパスを Miyabi 用に直し、2D 追跡 → 3D 化 → 固定較正での指令（と所望軌道）との比較まで。1 ジョブ最大 9 run、約 1 時間 |
| 較正 | ジョブの最後に、固定のカメラ→PAT 変換 `camera_to_pat_pooled_ffheart_20260918.npz` で比較を作る（別の変換を使うときは qsub に `TRANSFORM_FIXED=<npz>` を足す） | カメラを動かしたら kcheck から作り直す（`run_ff_heart_refit.sh`） |

## 3b. 役割の分担（重複させないこと）

| 段階 | どこで | 誰が |
|---|---|---|
| 収録 | 計測 PC | 計測者 |
| 転送、ジョブ投入（2D 追跡 → 3D 化 → 指令との比較） | 計測 PC → Miyabi（`miyabi_upload.py`） | 計測者 |
| 比較結果の取り込み（1 run 約 7 MB）、評価、図 | Mac（`real_data/ff_heart_20260918/fetch_and_eval.sh <一覧ファイル名>` など） | Claude |

**同じ run を SSD 経由でも送らないこと。** 送り直しは同名フォルダを消してから展開するので、処理中のジョブの入力を壊す（2026-09-21 に kcheck 1 本で起きた）。
Claude には「records フォルダ名、時刻の範囲、表示された一覧ファイル名（`list_<日時><a,b…>.txt`）」を伝える。

- Miyabi の同時実行は利用者あたり 2 ジョブ。3 本目以降は前のジョブが終わるまで待つ（qstat の開始予定は「前のジョブの制限時間いっぱい」で計算されるので遅く見えるが、実際は前のジョブが終わり次第始まる）。
- Miyabi-G（ARM）も同じ /work を共有し、Miyabi-C のログインノードから `qsub -q regular-g` で投入できる。ただし今の 3D 化の環境（intel python、x86 用の OpenCV）は Miyabi-C 用で、
  Miyabi-G で動かすには ARM 用の Python 環境を別に用意する必要がある（確認中。用意できたら本書に追記する）。

## 4. 注意

- **PowerShell の `|` でバイナリを流さない**（文字列として扱われて壊れる）。このスクリプトは Python から ssh を直接起動するので問題ない。手で tar を流すなら `cmd /c` の中で。
- 2 段階認証の入力は ssh が直接コンソールから読む。もし入力欄が出ずに止まる場合は、WSL（Ubuntu）の中で同じスクリプトを実行する（WSL なら接続の共有 `ControlMaster auto` も使える）。
- 送信中に回線が切れたら、その batch をもう一度実行すればよい（同名の run は消してから展開し直す）。
- 計測 PC のディスクから生データを消すのは、`OK` の表示と 3D 化の完了（Claude 側の報告）を確認してから。SSD は予備の保管として残す運用を勧める。
- 収録時のメモ: 室温、開始 LED の検出イベント数（`peak_to_threshold_ratio` が 3 未満なら撮り直し）、休止時間。

## 5. 動作確認の記録

2026-09-21 に Mac から同じスクリプトで自己テスト済み（ダミーの 2 run を送信 → グループ xg25g006、`._*` の除外、サイズ照合 OK、一覧ファイルの作成まで）。
Windows での実行と、Windows の ssh の 2 段階認証の入力はまだ試していない（最初の 1 回は `--no-qsub` を付けて小さい run で試すこと）。

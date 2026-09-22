# 計測データをMiyabiへ転送する（計測PCから直接）

作成: 2026-09-21

`G:\マイドライブ\Experiment\20260920\measurement_plan\handoff_acquisition_pc\ACQUISITION_PC_UPLOAD.md`
の手順を、このフォルダで実行できるようにしたものです。転送スクリプト `miyabi_upload.py` は
同フォルダからの写し（SHA-256一致）で、手順書の写しは `REFERENCE_ACQUISITION_PC_UPLOAD.md` です。

従来の「計測PC → SSD → Mac → Miyabi」をやめ、計測PCからMiyabiへ直接送って3D化ジョブまで投入します。

## 1. 最初の1回だけ必要な準備（**未完了です**）

このPCには `ssh`（OpenSSH 10.3）は入っていますが、Miyabi用の鍵と設定がまだありません。
次の3つはユーザーの作業です（鍵の作成と登録は私からは行いません）。

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\id_ed25519_miyabi"
```

1. 上のコマンドで鍵を作る（既存の `id_ed25519` とは別に、Miyabi用を新しく作るのが手順書の指示です）。
2. できた `id_ed25519_miyabi.pub` の中身を、Miyabiの利用者ポータルで登録する。
3. `%USERPROFILE%\.ssh\config` に次を追記する。

```text
Host miyabi-c.jcahpc.jp
  User x10733
  IdentityFile ~/.ssh/id_ed25519_miyabi
  ServerAliveInterval 30
```

そのうえで一度 `ssh miyabi-c.jcahpc.jp` で入り、2段階認証まで通ることを確認してください。
WindowsのOpenSSHは接続を共有できないので、**ssh 1回につき2段階認証が1回**必要です。
転送スクリプトは1バッチ（最大9 run）を1回のsshで送るので、認証は9 runにつき1回で済みます。

## 2. 毎回の手順

```powershell
# 送る対象の確認（実際には送りません）
.\venv\Scripts\python.exe .\miyabi_upload.py stereo_acoustools_3d_records_ff_heart --since 20260921_0000 --dry-run
```

```powershell
# 送信＋3D化ジョブの投入
.\venv\Scripts\python.exe .\miyabi_upload.py stereo_acoustools_3d_records_ff_heart --since 20260921_0000
```

**初めての実機転送は、小さいrun 1本を `--no-qsub` で試してください**（手順書の指示。Windowsでの実行と
2段階認証の入力はまだ試されていません）。

```powershell
.\venv\Scripts\python.exe .\miyabi_upload.py stereo_acoustools_3d_records_large_step --match XL12 --no-qsub
```

| オプション | 説明 |
|---|---|
| `--since 20260921_0000` | その時刻以降のrunだけ（前方一致可） |
| `--match 文字列` | run名に含まれる文字列で絞る |
| `--dry-run` | 対象の一覧だけ表示。sshは起動しません |
| `--no-qsub` | 送るだけでジョブを投入しない |
| `--gzip-level 1` | 圧縮率（既定1。上げると遅くなります） |

- `feedforward_validation` のrunが先、あとは時刻順に送られます。9 runごとに1ジョブです。
- 送りながらgzipで圧縮します（イベントデータは約1/3）。1 run（1.5 GB）で1分前後の見込みです。
- 最後にrunごとの合計バイト数をMiyabi側と照合し、`OK` か `MISMATCH` を表示します。
  `MISMATCH` のrunだけ `--match` で送り直してください。
- `QSUB 34xxxxx.opbs` と出ればジョブ投入済みです。3D化は1ジョブ約50〜60分、同時実行は2ジョブまでです。
- **生データを消すのは、`OK` の表示と3D化の完了を確認してからにしてください。**

## 3. Miyabi側でスクリプトが行うこと

| 項目 | 内容 |
|---|---|
| 置き場所 | `/work/xg25g006/x10733/eventcam/stereo_3d/<recordsフォルダ名>/<run>` |
| グループ | 展開後に `chgrp -R xg25g006`、フォルダに `chmod g+s`（個人グループの/workは50 GB上限のため） |
| 一覧 | `list_<日時><a,b,…>.txt`（1行1 run） |
| ジョブ | `qsub -q regular-c -N up_<…> -v LIST=<一覧>,NPAR=<本数> run_post_and_compare_list.pbs`（2D追跡 → 3D化 → 固定変換 `camera_to_pat_pooled_ffheart_20260918.npz` での指令 u・所望軌道 r との比較まで。2026-09-21 03:06のG:版。それ以前の写しは `run_ff_heart_post_list.pbs` で、比較を含まなかった） |
| 役割分担 | 比較結果の取り込み・評価・図は解析側（Mac）。**同じrunをSSD経由でも送らないこと**（送り直しは同名フォルダを消してから展開するので、処理中のジョブの入力を壊す）。解析側には records フォルダ名・時刻の範囲・一覧ファイル名 `list_<日時><a,b…>.txt` を伝える |

## 4. 注意

- **PowerShellの `|` でバイナリを流さないでください**（壊れます）。このスクリプトはPythonからsshを直接
  起動するので問題ありません。
- 2段階認証の入力はsshがコンソールから直接読みます。入力欄が出ずに止まる場合は、WSLの中で同じ
  スクリプトを実行してください。
- 送信中に切れたら、同じコマンドをもう一度実行すれば大丈夫です（同名のrunは消してから展開し直します）。
- 収録時のメモ（室温、LEDの検出イベント数、休止時間）も残してください。

## 5. このPCでの確認状況

- `miyabi_upload.py` と手順書はG:からの写しで、SHA-256が一致します。
- `--dry-run` は動作します（例: `stereo_acoustools_3d_records_large_step` で8 run・7.2 GB）。
- `test_miyabi_upload.py`（8件）で、Windowsのパス区切りがtarの中で `/` になること、`._*` を除くこと、
  グループ変更・一覧・qsubのコマンド、9 runごとの分割、サイズ不一致の検出、`--no-qsub`、`--since`／
  `--match` の絞り込みを、sshもネットワークも使わずに確認しています。
- **実機のsshによる転送はまだ行っていません**（鍵の登録が未了のため）。

```powershell
.\venv\Scripts\python.exe -B -m unittest test_miyabi_upload -v
```

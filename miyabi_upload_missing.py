#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Miyabi にまだ無い run だけを送る（miyabi_upload.py の送信処理をそのまま使う）。

  python miyabi_upload_missing.py --dry-run                     # 差分の一覧だけ（ssh 1 回: 一覧の取得）
  python miyabi_upload_missing.py                               # 差分を送信＋3D化ジョブ投入
  python miyabi_upload_missing.py stereo_acoustools_3d_records_ff_heart --no-qsub
  python miyabi_upload_missing.py --remote-list miyabi_list.txt # 取得済みの一覧を使う（ssh を 1 回節約）

ssh は「一覧の取得 1 回 ＋ 9 run ごとに 1 回」。Miyabi は公開鍵の後にワンタイムパスワードを求めるので、
端末が無い場所から実行するときは miyabi_askpass.cmd を SSH_ASKPASS に指定する（入力用の小窓が出る）。

PuTTY で開いてある接続に相乗りすることもできる（そのセッションで「Share SSH connections if possible」を
有効にしてから接続しておくこと。認証は最初の 1 回だけで済む）:
  python miyabi_upload_missing.py --plink x10733@miyabi-g.jcahpc.jp
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

import miyabi_upload as up

PLINK = r"C:\Program Files\PuTTY\plink.exe"

LIST_REMOTE = (
    f"cd {up.WORK} && for d in */; do for r in ${{d}}*/; do echo RUN ${{r}}; done; done"
)


def remote_runs(text):
    """{records フォルダ名: {run 名}}。ワイルドカードが残った行（空フォルダ）は無視する。"""
    found = {}
    for line in text.splitlines():
        m = re.match(r"^RUN (.+?)/(.+?)/\s*$", line.strip())
        if m and "*" not in m.group(2):
            found.setdefault(m.group(1), set()).add(m.group(2))
    return found


def use_plink(target):
    """ssh の代わりに plink を使い、PuTTY で開いてある接続に相乗りする。

    miyabi_upload.send_batch は ["ssh", "-o", ..., HOST, remote] の形で起動するので、
    その argv を plink 用に置き換える（標準入力の tar はそのまま流れる）。
    """
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        if isinstance(argv, list) and argv and argv[0] == "ssh":
            argv = [PLINK, "-share", "-batch", target, argv[-1]]
        return real_popen(argv, **kwargs)

    up.subprocess.Popen = popen
    globals()["SSH"] = [PLINK, "-share", "-batch", target]


def fetch_remote_runs():
    ssh = globals().get("SSH", ["ssh", "-o", "ServerAliveInterval=30", up.HOST])
    print(f"--- Miyabi の一覧を取得します: {ssh[0]} -> {up.WORK}", flush=True)
    done = subprocess.run(ssh + [LIST_REMOTE], stdout=subprocess.PIPE)
    if done.returncode != 0:
        sys.exit(f"一覧の取得に失敗しました（ssh の終了コード {done.returncode}）")
    return remote_runs(done.stdout.decode(errors="replace"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("records", nargs="*", type=Path, help="対象の records フォルダ（省略時は stereo_acoustools_3d_records_* すべて）")
    ap.add_argument("--remote-list", type=Path, default=None, help="取得済みの一覧ファイル（RUN <records>/<run>/ の行）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-qsub", action="store_true", help="送るだけでジョブは投入しない")
    ap.add_argument("--gzip-level", type=int, default=1)
    ap.add_argument("--plink", metavar="USER@HOST", default="",
                    help="PuTTY で開いてある接続に相乗りする（例: x10733@miyabi-g.jcahpc.jp）")
    a = ap.parse_args()
    if a.plink:
        use_plink(a.plink)

    roots = [p.resolve() for p in a.records] if a.records else sorted(
        p for p in Path.cwd().glob("stereo_acoustools_3d_records_*") if p.is_dir()
    )
    if not roots:
        sys.exit("records フォルダが見つかりません")
    there = remote_runs(a.remote_list.read_text(encoding="utf-8", errors="replace")) if a.remote_list else fetch_remote_runs()

    plan = []
    for records in roots:
        local = sorted((d for d in records.iterdir() if d.is_dir() and up.stamp(d.name)), key=lambda d: up.stamp(d.name))
        have = there.get(records.name, set())
        missing = [d for d in local if d.name not in have]
        print(f"{records.name}: 手元 {len(local)} / Miyabi {len(have)} / 未アップ {len(missing)}")
        for d in missing:
            print(f"    - {d.name}  {up.local_bytes(d) / 1e9:.2f} GB")
        if missing:
            plan.append((records, missing))
    total = sum(up.local_bytes(d) for _records, runs in plan for d in runs)
    if not plan:
        print("未アップの run はありません。")
        return
    print(f"合計 {sum(len(r) for _x, r in plan)} run、{total / 1e9:.1f} GB")
    if a.dry_run:
        return

    allok = True
    for records, runs in plan:
        # 解析で先に要るものを先に送る（miyabi_upload.py と同じ並び）
        runs = [r for r in runs if r.name.startswith("feedforward")] + [r for r in runs if not r.name.startswith("feedforward")]
        base = up.stamp(runs[0].name).replace("_", "")[4:12]
        for i in range(0, len(runs), up.PER_JOB):
            tag = f"{base}{chr(97 + i // up.PER_JOB)}"
            allok &= up.send_batch(records, runs[i:i + up.PER_JOB], tag, a.no_qsub, a.gzip_level)
    print("\n全 run のサイズが一致しました。" if allok else "\nサイズが合わない run があります。その run だけ送り直してください。")


if __name__ == "__main__":
    main()

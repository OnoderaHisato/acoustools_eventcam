#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""計測 PC から Miyabi へ、収録した run を直接送って 3D 化ジョブを投入する（標準ライブラリだけで動く。Windows / macOS / Linux 共通）。

  python miyabi_upload.py <records フォルダ> --since 20260921_2200            # その時刻以降の run を全部
  python miyabi_upload.py <records フォルダ> --match wscan_XZ                 # 名前に文字列を含む run だけ
  python miyabi_upload.py <records フォルダ> --since 20260921_2200 --dry-run  # 送る対象の一覧だけ表示

やること（ssh 接続は 1 バッチにつき 1 回 = 2 段階認証も 1 回）:
  1. run フォルダを tar + gzip の流れにして ssh の標準入力へ送り、Miyabi 側でそのまま展開する（途中のファイルを作らない）
  2. 展開後にグループを xg25g006 に変える（個人グループ x10733 の /work は 50 GB 上限で、超えると書き込みが途中で失敗する）
  3. run ごとの合計バイト数を Miyabi 側で数えて返させ、手元の値と照合する
  4. run の一覧ファイルを作り、3D 化ジョブを投入する（1 ジョブ最大 9 本。Miyabi の同時実行は 2 ジョブまでで、超えた分は順番待ち）
PowerShell のパイプ（|）はバイナリを壊すので使わないこと。このスクリプトは Python から ssh を直接起動するので安全。
"""
import argparse, gzip, os, re, subprocess, sys, tarfile
from pathlib import Path

HOST = "x10733@miyabi-c.jcahpc.jp"
WORK = "/work/xg25g006/x10733/eventcam/stereo_3d"
GROUP = "xg25g006"
PBS = "run_post_and_compare_list.pbs"   # 2D 追跡 → 3D 化 → 固定較正での指令との比較まで 1 ジョブで行う
PER_JOB = 9


def stamp(name):
    m = re.search(r"(\d{8}_\d{6})$", name); return m.group(1) if m else ""


def local_bytes(run):
    return sum(f.stat().st_size for f in run.rglob("*") if f.is_file() and not f.name.startswith("._"))


def send_batch(records, runs, tag, no_qsub, level):
    rec = records.name; dst = f"{WORK}/{rec}"; names = " ".join(r.name for r in runs); lst = f"list_{tag}.txt"
    remote = (f"mkdir -p {dst} && chgrp {GROUP} {dst} && chmod g+s {dst} && cd {dst} && rm -rf {names} && tar xzf - --no-same-permissions --no-same-owner && "
              f"chgrp -R {GROUP} {names} && find {names} -type d -exec chmod g+s {{}} + ; "
              f"for d in {names}; do echo SIZE $d $(find $d -type f -printf '%s\\n' | awk '{{s+=$1}} END{{print s}}'); done; "
              f"cd {WORK} && : > {lst} && for d in {names}; do echo {rec}/$d >> {lst}; done; "
              + ("echo QSUB skipped" if no_qsub else f"echo QSUB $(qsub -q regular-c -N up_{tag} -v LIST={lst},NPAR={len(runs)} {PBS})"))
    print(f"--- batch {tag}: {len(runs)} run。ssh の認証（2 段階）を求められたら入力してください")
    p = subprocess.Popen(["ssh", "-o", "ServerAliveInterval=30", HOST, remote], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    with gzip.GzipFile(fileobj=p.stdin, mode="wb", compresslevel=level) as gz, tarfile.open(fileobj=gz, mode="w|") as tf:   # Python 3.8 以降で共通に動く形
        for r in runs:
            print(f"    sending {r.name}", flush=True); tf.add(r, arcname=r.name, filter=lambda ti: None if Path(ti.name).name.startswith("._") else ti)
    p.stdin.close(); out = p.stdout.read().decode(errors="replace"); p.wait(); ok = True
    sizes = dict(re.findall(r"SIZE (\S+) (\d+)", out))
    for r in runs:
        loc = local_bytes(r); rem = int(sizes.get(r.name, -1)); good = loc == rem; ok &= good
        print(f"    {'OK      ' if good else 'MISMATCH'} {r.name}  local {loc}  remote {rem}")
    print("    " + (re.search(r"QSUB.*", out).group(0) if "QSUB" in out else f"(ジョブ投入の応答なし。ssh の終了コード {p.returncode})")); return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("records", type=Path, help="例: stereo_acoustools_3d_records_ff_heart（収録スクリプトの --output-dir）")
    ap.add_argument("--since", default="", help="この時刻（YYYYMMDD_HHMMSS、前方一致可）以降の run だけ"); ap.add_argument("--match", default="", help="名前に含まれる文字列")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--no-qsub", action="store_true", help="送るだけでジョブは投入しない"); ap.add_argument("--gzip-level", type=int, default=1)
    a = ap.parse_args(); rec = a.records.resolve()
    runs = sorted([d for d in rec.iterdir() if d.is_dir() and stamp(d.name) and stamp(d.name) >= a.since and a.match in d.name], key=lambda d: stamp(d.name))
    if not runs: sys.exit("対象の run がありません")
    # 解析で先に要るもの（feedforward_validation）を先に送る
    runs = [r for r in runs if r.name.startswith("feedforward")] + [r for r in runs if not r.name.startswith("feedforward")]
    total = sum(local_bytes(r) for r in runs); print(f"{len(runs)} run、合計 {total / 1e9:.1f} GB → {HOST}:{WORK}/{rec.name}")
    for r in runs: print("   ", r.name)
    if a.dry_run: return
    allok = True; base = stamp(runs[0].name).replace("_", "")[4:12]
    for i in range(0, len(runs), PER_JOB): allok &= send_batch(rec, runs[i:i + PER_JOB], f"{base}{chr(97 + i // PER_JOB)}", a.no_qsub, a.gzip_level)
    print("\n全 run のサイズが一致しました。" if allok else "\nサイズが合わない run があります。その run だけ --match で送り直してください。")


if __name__ == "__main__":
    main()

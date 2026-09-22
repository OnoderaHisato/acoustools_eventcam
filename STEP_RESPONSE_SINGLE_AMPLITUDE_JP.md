# 単一振幅ステップ応答計測（保持2.0秒）

作成: 2026-09-16

既存の `STEP_RESPONSE_MEASUREMENT_JP.md` の Tier A〜H を置き換えるものではなく、
同じ staircase 生成経路・同じ安全検査の上に追加する計測系列です。

## 既存 Tier との違い

| | 既存 Tier A〜H | 本系列 |
|---|---|---|
| 1 run 内の振幅 | 複数（例 C は 0.30/0.60/0.85/1.05 mm） | 1種類のみ |
| 符号の順序 | `order_seed` でブロック内シャッフル | 決定論的な完全交互（seed なし） |
| 保持時間 | 0.3 s（A〜D）/ 1.0 s（E〜H） | 全て 2.0 s |
| 1 run | 6.8〜18.0 s | 26.0 s |
| 狙い | 振幅依存性の走査 | 同一ステップの多数回平均と、減衰の完全な観測 |

保持を 2.0 s に伸ばした理由は、0.3 s では減衰が終わらず次のジャンプの初期条件が
ゼロにならない可能性があるためです。2.0 s あれば、時定数 160 ms（100 Hz・Q=50 相当）
でも 12.5τ となり、残留はほぼ無視できます。

## 指令の形

全 run 共通で、振幅 A の符号だけが交互に入れ替わります。

```
0(2.0s) → +A(2.0s) → 0(2.0s) → −A(2.0s) → 0(2.0s) → +A(2.0s) → 0(2.0s)
        → −A(2.0s) → 0(2.0s) → +A(2.0s) → 0(2.0s) → −A(2.0s) → 0(2.0s)
```

- hold 13個、ジャンプ 12回、26.0 s、10 kHz で 260,000 フレーム
- ジャンプ時刻はちょうど 2.0, 4.0, …, 24.0 s
- 各符号に 3回到達、中心への復帰も 3回 → 立ち上がり 3本・立ち下がり 3本 × 2符号
- 目標間の直接ジャンプは無し。必ず中心を経由します
- 補間・ランプ無し（`ramp_sec: 0.0`）。次の 100 µs フレームで切り替わります

## ファイル一覧

各振幅を別ファイルにしてあります。`safety_limits` をその振幅ちょうどに固定できるため、
生成側の不具合でより大きなジャンプが出た場合に、記録される前に例外で止まります。

| ファイル | 振幅 | 力最大点比 | 脱出境界比 | 実績 Tier | run |
|---|---:|---:|---:|---|---:|
| `step_response_single_amplitude_0p25_plan.json` | 0.25 mm | 23.3% | 11.7% | A | 3 |
| `step_response_single_amplitude_0p40_plan.json` | 0.40 mm | 37.3% | 18.7% | A | 3 |
| `step_response_single_amplitude_0p70_plan.json` | 0.70 mm | 65.3% | 32.7% | B | 3 |
| `step_response_single_amplitude_1p05_plan.json` | 1.05 mm | 98.0% | 49.0% | C | 3 |
| `step_response_single_amplitude_1p40_plan.json` | 1.40 mm | 130.6% | 65.3% | E | 3 |
| `step_response_single_amplitude_1p70_plan.json` | 1.70 mm | 158.6% | 79.3% | E | 3 |

力最大点 1.072 mm、推定脱出境界 2.144 mm（40 kHz・c=343 m/s）。
各ファイルは X/Y/Z の 3 run で、合計 18 run・PAT再生のみで 7.8分。

振幅は全て 2026-08-21 に該当 Tier で計測済みかつ粒子生存確認済みのものだけを選んでいます。
新規なのは保持時間だけです。

## 生成（実機を開きません）

```powershell
.\venv\Scripts\python.exe .\hf_identification_trajectory.py `
  --plan .\step_response_single_amplitude_0p25_plan.json `
  --output-dir .\step_response_single_amplitude_export_0p25
```

staircase は `--write-csv` を付けなくても `command_offset_log.csv` を必ず出力します。
1 run あたり 260,000 行になるため、出力先の空き容量を確認してください。
残り5つの振幅も同様に、振幅ごとに別の `--output-dir` を指定します。

## ハードウェアを開かない確認

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_single_amplitude_export_0p25 `
  --preview-only

.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_single_amplitude_export_0p25 `
  --output-dir .\stereo_acoustools_3d_records_step_response `
  --dry-run
```

## 全18 runの一括自動計測（2026-09-16追加）

生成、dry-run、実機計測を 1 本のスクリプトで実行できます。PAT/OpenMPD の接続は 1 回だけで、
全18 runを振幅の小さい順（各振幅内は X → Y → Z）に連続して記録します。

```powershell
# 実機を開かない確認（未生成の export はここで生成されます）
powershell -ExecutionPolicy Bypass -File .\run_step_response_single_amplitude_all.ps1 -DryRunOnly

# 実機計測
powershell -ExecutionPolicy Bypass -File .\run_step_response_single_amplitude_all.ps1
```

動作:

- 既存の export（`export_manifest.json` があるもの）は再生成しません。作り直す場合は `-Regenerate`。
- 最初の run（S025 X）のホログラムを **PAT を出力する前に** 計算します。
  ホログラム計算の所要時間はここで分かります。ただし後述の
  `prepare_message_from_holograms` のメモリピークは PAT 起動後、記録直前に発生します。
- PAT 起動後、最初の run だけステレオプレビューと Enter 確認があります。
  粒子が中心で安定し、左右両眼に見えることを確認して Enter を押してください。
- 2 run目以降はプレビューも Enter もなく進みます。各 run の後に粒子は中心へ戻り、
  次の run の準備（送信データの作成など、1 run 約30秒）の間も中心で保持されます。
- LED 検出失敗、カメラの同期開始失敗、記録プロセスの異常終了、PAT 送信失敗のいずれかが
  起きた場合は、同じ run を用意済みホログラムのまま最大2回まで自動で再計測します。
  それでも失敗したらセッション全体を止め、PAT 出力を停止します。
- 2026-09-16の実測では1 runあたり約100秒（うちホログラム計算約33秒）でした。
  ホログラムの使い回し（後述）により、約70秒・18 runで約21分になる見込みです。

注意: **粒子の脱落は自動では検知しません。** 途中で粒子が落ちても、残りの run は
空の記録として続きます。各 run の `pipeline_manifest.json` の
`automation.operator_checkpoint_before_capture` で、確認付きの run（最初の1本）と
無人の run を区別できます。自動再計測の履歴は `capture_retry` に残ります。

一部の振幅だけ、または再試行回数を変える場合:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_step_response_single_amplitude_all.ps1 `
  -Amplitudes "1p05,1p40,1p70" -AutomaticCaptureRetries 1
```

`-Amplitudes` の書き順に関係なく、実行は常に小さい振幅から行います。出力先の既定は
`.\stereo_acoustools_3d_records_step_response_2s` で、`-OutputDir` で変更できます。

スクリプトを使わずに同じことを直接実行する場合は、`--hf-export-dir` を振幅の数だけ並べます。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\step_response_single_amplitude_export_0p25 `
  --hf-export-dir .\step_response_single_amplitude_export_0p40 `
  --hf-export-dir .\step_response_single_amplitude_export_0p70 `
  --hf-export-dir .\step_response_single_amplitude_export_1p05 `
  --hf-export-dir .\step_response_single_amplitude_export_1p40 `
  --hf-export-dir .\step_response_single_amplitude_export_1p70 `
  --output-dir .\stereo_acoustools_3d_records_step_response_2s `
  --unattended-after-first-checkpoint `
  --acknowledge-step-response-risk
```

`--unattended-after-first-checkpoint` は、選択した run が全て step-response staircase の
場合だけ使えます。Tier H、`--confirm-each-run`、`--preview-each-run`、`--keep-going` とは
併用できません。

## 実機計測の順序（1 runずつ手動で進める場合）

1. `0p25` の X から 1 run だけ記録し、後述のメモリと準備時間を実測で確認する
2. 同じファイルの Y、Z
3. 12ジャンプ全てで粒子が左右両眼に残っていることを確認してから次の振幅へ
4. 以降 `0p40` → `0p70` → `1p05` → `1p40` → `1p70` の順

実機コマンドには全振幅で`--acknowledge-step-response-risk`だけを付けます。plan の`tier`（A/B/C/E）による追加フラグはありません（2026-09-16に廃止）。

振幅を飛ばさないでください。既存 Tier で生存確認済みとはいえ、保持時間が変わると
粒子が新しい平衡点に完全に落ち着いてから戻すことになり、往復の履歴が変わります。

## 事前に確認すること：メモリと準備時間

1 run が 260,000 フレームで、これまでの最大だった Tier E の 180,000 フレームの 1.44倍です。

- ホログラムは位置の種類ごとに1回だけ計算し、同じ位置のフレームで使い回します（2026-09-16変更）。
  staircase の位置は中心・+A・−A の3種類なので、`kd_solver` は1 runあたり3回です。
  変更前は260,000回で約33秒かかっていました。各フレームへ渡るホログラムは変更前と同じ値です。
- `prepare_message_from_holograms` は ctypes 化の前に、
  260,000 × 512 = 1.33億個の Python float をリストとして位相用・振幅用に1本ずつ作ります。
  概算でピーク 8〜9 GB です（ホログラムのテンソルは3個を共有するので、フレームごとに複製していた分のメモリは不要になりました）。

`static_compacted` は軌道全体が完全に静止している場合にしか効かないので、
staircase では圧縮されません。最初の 1 run でメモリ不足や準備時間が問題になる場合は、
次のどちらかで対応してください。

- `repeats_within_run` を 3 → 2 に下げる（18.0 s・180,000フレーム。Tier E と同サイズで実績あり）
- `prepare_message_from_holograms` を、巨大な Python リストを経由せず
  ctypes 配列へ直接書き込む実装に変更する（実機送信経路の変更なので要相談）

## 安全設計

- 各ファイルの `max_offset_mm` と `max_step_mm` はその振幅ちょうど
- `_staircase_safety_check` により、脱出境界 2.144 mm 以上のジャンプは例外
- `allow_escape_boundary_probe` は設定していません（Tier H 専用）
- 速度・加速度制限は staircase では意図的に適用されません。
  不連続指令の数値微分は物理的な安全指標にならないためです

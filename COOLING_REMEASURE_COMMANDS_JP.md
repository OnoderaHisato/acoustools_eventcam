# 冷却後の再計測（Experiment/20261006）の実行コマンド

計画の正本は G: の `Experiment/20261006/COOLING_REMEASURE_PLAN_20261006.md`。スクリプトは `run_cooling_remeasure_15v.ps1`。
記録は `stereo_acoustools_3d_records_V15c`（18 V は `V18c`）に入り、9/23〜9/25 の記録とは混ざらない。
どのコマンドもこのフォルダ（`eventcam_control`）で、PowerShell から実行する。

ブロック 2c・2d の `_Ws4` 6本は、解析側の 2026-10-07 の依頼で、地図の平滑化 σ 0.3 mm の版（`ff_heart_15V_sig03`）に差し替え済み。コマンドは変わらない。

## 0. 前日まで・当日の最初に

- 開始LEDを明るくする（kcheck の LED のピークを 1000 以上に）。
- 電源を 15 V にする。電源のUSB（KI-VISA）をつなぐ（`-PsuUsb` で記録と電流の見張りが動く）。
- 温度の記録用紙（G: の `thermal_log_template.csv` に `fan_state`、`fan_rpm_or_V` の列を足したもの）。
- 実機を開かない確認（全ブロック）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 0 -DryRunOnly
```

（`-Block 1`、`-Block 2`、`-Block 12` も同じ形で確認できる）

## 1. ブロック 0 — ファンの影響（冷えた状態、ファン OFF で始める、約15分、9本）

始める前に、音を出さずにファンを ON にして、板のあいだにティッシュの細片を近づけ、風が入らないことを確かめる。確かめたらファンは OFF に戻す。

```powershell
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 0 -PsuUsb
```

- 流れ: hold 30 s（ファン OFF）→ kcheck x/y/z →「ファンを ON にしたら Enter」→ hold 30 s（ON）→ kcheck x/y/z →「ファンを OFF にしたら Enter」→ hold 30 s（OFF）。
- Enter の時刻は session JSON の `operator_actions` に残る。
- 終わったら、解析側の判定（ファンの線が出ていないか、揺れと剛性の差）を待つ。線が出たら、ファンの回転数か取り付けを直してからブロック 1 へ進む。

## 2. ブロック 1 — 熱の確認（ファン ON、冷えた状態から 90 分、39本）

ブロック 0 のあと、音を止めて 45 分以上冷ましてから。ファンは ON。

ブロック 1 だけ撮る場合:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 1 -PsuUsb
```

ブロック 1 に続けて、そのままブロック 2 も撮る場合（計画の推奨。粒子は浮かせたまま、合計 114 本、約 2 時間 40 分）:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 12 -PsuUsb
```

- 時刻表（音を出してからの分）: 0〜60 分は 5 分ごとに kcheck x/z。20・60・90 分は x/y/z。0・30・60・90 分に hold 10 s。45・85 分に XZ 走査（半径 28 mm）a/b。
- 音を出した直後に粒子の確認（プレビューと Enter）があり、あとは時刻どおりに進む。
- 5 分ごとに温度（室温、板の裏の上・下、板のあいだの空気）を記録用紙に書く。

## 3. ブロック 2 — NN の学習用データ（約 69 分、78 本）

ブロック 1 を別の日に済ませた場合だけ使う（同じ日なら上の `-Block 12`）。`-WarmupMin` はブロック 1 で分かった「落ち着くまでの時間」（分）。

```powershell
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 2 -WarmupMin 20 -PsuUsb
```

- 2b の 14 本の走査のあとに、遅い走査 `wscan_XZ_R28slow_a`／`_b`（0.75 回/s、各 48 s）が入る。この 2 本は記録の末尾の余裕が自動で約 9 s に延びる。省くときは `-SkipSlowScan` を付ける（ブロック 2・12 で使える）。
- 2d の終わりに、大振幅の `cardioid_a17_f10_OFF` と `cardioid_a23_f10_OFF` が入る（a23 の前で確認）。
- 2e のあとに、任意の 2f（学習した模型の補償指令 7 本、約 8 分）が入る。省くときは `-SkipNnTest` を付ける（ブロック 2・12 で使える）。
- 確認（プレビューと Enter）で止まるところ: 0.2 mm の x の掃引の前、0.2 mm の y の掃引の前、3.0 mm のステップ（XL30）の前、a23 の前。粒子が落ちていたら Ctrl+C。
- 最後に「粒子を替えたら Enter」が 2 回出る。時刻は session JSON の `particle_changes` に残る。

## 3b. 任意 — 2f（学習した模型の補償指令）だけを撮る（14本、暖機のあと約12分）

ブロック 2 で `-SkipNnTest` を付けて 2f を省いた日や、別の日に 2f だけ撮るとき。ファン ON、冷えた状態から。

```powershell
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 2f -WarmupMin 20 -PsuUsb
```

- 流れ: 音を出した直後に粒子の確認 → `-WarmupMin` 分の暖機（既定 20 分）→ kcheck x/y/z → hold 10 s → 2f の 7 本 → kcheck x/y/z。
- 暖機を省いてすぐ始めるときは `-WarmupMin 0`（熱が上がっている途中の記録になる）。
- 比べる相手の OFF は別のセッションのものになる。

## 4. 任意 — 18 V の試験（ブロック 1 が通ったあと、別の日か 45 分以上の休止のあと、60 分、31 本）

電源を 18 V にしてから:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 1 -SupplyVoltage 18 -DurationMin 60 -PsuUsb
```

記録は `stereo_acoustools_3d_records_V18c` に入る（15 V のデータと混ぜない）。

## 止まったとき

- 電流の見張り（+3 %）が止めた場合: 「冷却が足りない」の記録になる。45 分以上休止してから、残りを撮る（撮り直しのコマンドは、止まった位置を見て Claude が用意する）。
- 粒子が落ちた・記録に失敗した場合も、session JSON を見て残りのコマンドを用意する。
- 撮り終えたら Claude に伝える（SSD へのバックアップ、3D 化、解析側への連絡）。

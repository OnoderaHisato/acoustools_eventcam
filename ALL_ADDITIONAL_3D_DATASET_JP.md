# 追加3D軌道・統合auto計測

## 統合JSON

`all_additional_3d_dataset_plan.json`は、既存計画を順番に読み込むinclude型JSONです。
条件を複製していないため、元JSONのパラメータを修正すると統合計画にも反映されます。

| 通し番号 | group | 元JSON | 条件数 | 指令運動時間 |
|---:|---|---|---:|---:|
| 0--18 | `01_extended` | `extended_3d_dataset_plan.json` | 19 | 114.25 s |
| 19--27 | `02_chirped` | `chirped_3d_dataset_plan.json` | 9 | 72.00 s |
| 28--39 | `03_cusped` | `cusped_3d_dataset_plan.json` | 12 | 20.40 s |
| 40--43 | `89_cusped_retention_boundary` | `cusped_3d_retention_boundary_plan.json` | 4 | 1.57 s |
| 44--47 | `90_chirped_retention_boundary` | `chirped_3d_retention_boundary_plan.json` | 4 | 20.00 s |

合計48条件です。内訳はtrain 28、validation 12、diagnostic 8です。
純粋な指令運動時間は約228.22秒ですが、実際には各runのホログラム計算、ステージ開始点への
移動、カメラ起動、tail margin、NPZ保存が加わるため、セッション時間は大幅に長くなります。

各runのautomation metadataと統合preview CSVには次を保存します。

- 統合JSON内の`json_index`
- 元JSON内の`source_json_index`
- `plan_group`
- `source_plan`

## dry-runと統合図

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\all_additional_3d_dataset_plan.json `
  --dry-run
```

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\all_additional_3d_dataset_plan.json `
  --preview-only `
  --preview-output-dir .\all_additional_3d_dataset_plan_preview
```

## 通常40条件を一括計測

trainとvalidationだけを選ぶと、保持限界8条件を除外して一括計測できます。

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\all_additional_3d_dataset_plan.json `
  --role train `
  --role validation `
  --output-dir .\stereo_acoustools_3d_records_all_additional `
  --acknowledge-extended-trajectory-safety
```

この場合、既定では最初の1回だけカメラプレビューを表示し、通常40条件を連続実行します。
全条件前に確認する場合だけ`--confirm-each-run --preview-each-run`を追加します。

## 保持限界を含む48条件を一コマンドで計測

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --json .\all_additional_3d_dataset_plan.json `
  --output-dir .\stereo_acoustools_3d_records_all_additional `
  --acknowledge-extended-trajectory-safety `
  --acknowledge-retention-boundary-risk `
  --allow-retention-boundary-tail
```

通常40条件は連続実行されます。通し番号40以降の保持限界8条件では、コマンド指定にかかわらず
毎回ステレオプレビューとEnter確認を強制します。粒子が見えない場合はpreviewを中断すると、
セッション全体が停止します。

安全上、保持限界を含む選択では次を強制します。

- retention-boundaryは計画末尾の連続区間だけ
- 各保持限界runでカメラpreviewを再表示
- 各保持限界runでEnter確認
- `--keep-going`は禁止
- 専用ackがない場合はPATを開く前に停止

最後の4条件は、ご指定の`chirped_3d_retention_boundary_plan.json`です。

## モニター後処理

別PowerShellで、計測より先に起動します。

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_auto_monitor.py `
  .\stereo_acoustools_3d_records_all_additional `
  --new-only
```

計測負荷を優先して終了後に処理する場合:

```powershell
.\venv\Scripts\python.exe .\stereo_acoustools_3d_auto_monitor.py `
  .\stereo_acoustools_3d_records_all_additional `
  --once
```

## 部分再実行

統合通し番号を使って途中から選択できます。

```powershell
# cusped通常条件から開始
--start-index 28

# chirped保持限界の最初の1条件だけ
--start-index 44 --limit 1
```

保持限界を単独選択する場合も、専用ackと`--confirm-each-run`、または
`--allow-retention-boundary-tail`が必要です。

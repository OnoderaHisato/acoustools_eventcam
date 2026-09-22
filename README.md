# acoustools_eventcam

AcousTools / OpenMPD とステレオイベントカメラを使った、音響浮揚・軌道制御・計測・解析用コードです。
同期先: <https://github.com/OnoderaHisato/acoustools_eventcam>

## 安全上の注意

実機を制御するコードが含まれます。PAT出力、カメラ起動、ステージ移動、録画を伴う入口は、接続先・電圧・校正・実験条件を確認してから実行してください。
Git操作は実機を起動しません。過去の計測成功やシミュレーション結果は、別の環境での安全性・浮揚成功を保証しません。

まず [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md) と [AGENTS.md](AGENTS.md) を確認してください。

## 主なコードと手順書

- `acoustools_send_phase_npy.py`: 保存済み位相NPYの検査・任意の実機送信。
- `acoustools_visualize_phase_field.py`: 各素子の位相と音場の可視化。
- `acoustools_builtin_visualiser_phase_field.py`: AcousTools標準Visualiserを利用する可視化。
- `hologram_optimization_package/`: ホログラム最適化・放射力評価。
- `hologram_reoptimized_eps10mm_rho24p8_20260910/`: 直径10 mm・密度24.8 kg/m³の再最適化コード。
- [MIYABI_UPLOAD_JP.md](MIYABI_UPLOAD_JP.md): 計測データをMiyabiへ直接転送し、3D化ジョブを投入する手順。`miyabi_upload.py`。
- [EVENTCAM_MEASUREMENT_EXTRACTION_GUIDE_JP.md](EVENTCAM_MEASUREMENT_EXTRACTION_GUIDE_JP.md): イベントカメラ計測・粒子抽出の必要ファイル、実行手順、スパコンへ渡すデータ。
- [eventcam_standalone/README_JP.md](eventcam_standalone/README_JP.md): AcousToolsなしで校正・同期撮影・粒子抽出・3D復元を行う独立キット。OpenEB環境構築の指示書も同梱。
- [STEREO_ACOUSTOOLS_3D_AUTO_JP.md](STEREO_ACOUSTOOLS_3D_AUTO_JP.md): 自動計測。
- [VZR_IDENTIFICATION_MEASUREMENT_JP.md](VZR_IDENTIFICATION_MEASUREMENT_JP.md): vzr同定（x/y＋z同時正弦駆動）の追加計測。`run_vzr_identification_all.ps1` で一括実行。
- [LARGE_STEP_MEASUREMENT_JP.md](LARGE_STEP_MEASUREMENT_JP.md): 水平の力の上限を測る大振幅1軸ステップ（1.2〜2.0 mm）。`run_large_step_all.ps1` で一括実行。
- [VZR_STEP_MEASUREMENT_JP.md](VZR_STEP_MEASUREMENT_JP.md): ステップ型のvzr計測（水平1軸とzの同時ジャンプ）。`run_vzr_step_all.ps1` で一括実行。
- [FF_HEART_VALIDATION_MEASUREMENT_JP.md](FF_HEART_VALIDATION_MEASUREMENT_JP.md): 事前計算済みFF指令（ハート7 mm・10 HzのOFF／A／C）を数値そのまま実機へ送る検証計測。`run_ff_heart_validation.ps1` で一括実行。
- [STEREO_ACOUSTOOLS_3D_PIPELINE_JP.md](STEREO_ACOUSTOOLS_3D_PIPELINE_JP.md): 録画と後処理。
- [WINDOWS_EVENTCAM_SETUP_v2.md](WINDOWS_EVENTCAM_SETUP_v2.md): Windows環境構築。
- `test_*.py`: 入力検査・計算・ワークフローなどの回帰テスト。実機接続テストではありませんが、全フォルダの一括収集は避け、対象を指定してください。

## 保存対象と別途必要なもの

このリポジトリはコード・手順書・計測計画／設定JSONのバックアップです。既存の実験経緯を記載したMarkdownも含みます。
`.gitignore` は許可リスト方式で、新しいフォルダは明示的に追加しない限り同期されません。

次のものはGitHubに保存せず、元のローカルファイルを保持します。

- NPY位相マップ、NPZ/RAW/CSVなどの計測データ、校正結果、画像・動画・PDF・ZIP。
- 最適化結果JSON、再最適化の `results/`・`source_snapshot/`、軌道exportと各種出力フォルダ。
- `venv/`、OpenEB、vcpkg、外部SDK・リポジトリ、同梱 `dependencies/`、キャッシュ、認証情報、会話ログのエクスポート、パスワード欄を含む過去のPiセットアップメモ。

したがって、cloneだけで実験環境が完全復元されるわけではありません。AcousTools・OpenMPDのネイティブライブラリ、カメラSDK、装置設定・校正、入力NPY等を別途用意してください。
NPY送信スクリプト自体は標準ライブラリ・NumPy・PyTorchとAcousToolsを使用します。
再最適化にはSciPy等に加えて元の `source_snapshot/` と入力データが必要です。除外ファイルをGit同期のために削除する必要はありません。
サブフォルダのサンプルや過去版は、現行のルート入口とは区別して使用してください。

現在のWindows環境ではPythonを `.\venv\Scripts\python.exe` で実行します。
実機を開かずにNPYを検査する例（NPYは別途用意）:

```powershell
.\venv\Scripts\python.exe .\acoustools_send_phase_npy.py .\phase_acoustools_regular_complex64.npy
```

この入口は `--send` を付けなければ検査のみです。検査通過は実機での浮揚・安全性の保証ではありません。

## 今後のGitHub同期

このフォルダで実行します。同期は手動であり、編集だけでは自動アップロードされません。

```powershell
git status --short
git add -- <変更したファイルの相対パス>
git diff --cached --stat
git diff --cached
git commit -m "変更内容の説明"
git push
```

`<変更したファイルの相対パス>` は実際のファイル名に置き換えてください。公開前に差分を確認し、認証情報や非公開データを含めないでください。
他のPCやGitHub上で変更した場合は、作業ツリーを整理してから `git pull --ff-only` で取得します。失敗した場合にforce pushや履歴の破棄で解消しないでください。
新しいコード用フォルダを追加するときは `.gitignore` の許可ルールも更新してください。

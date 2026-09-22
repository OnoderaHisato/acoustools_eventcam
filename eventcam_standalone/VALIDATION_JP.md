# 検証記録

2026-09-16、Windows 11上のPython 3.11.9、NumPy 1.26.4、OpenCV 4.10.0、Matplotlib 3.10.9で確認しました。

## 実施した検証

実機不要のテスト9件がすべて成功しました。開発元では対象を限定した `test_eventcam_standalone` を実行し、キット利用先では同梱 `selftest.py` から同じテストを実行できます。

1. 同期OFF、7.1 mm、範囲外保存先、左右同一serial、非有限撮影時間、整数設定への不適切な値を拒否。
2. `all --dry-run` がファイルを作らず、子プロセスも開始しないことを確認。
3. 全工程がキット内のスクリプトだけを参照し、呼出し先に実在する引数を渡すことを確認。
4. 全Pythonコードのimportを検査し、親プロジェクトのモジュール依存がないことを確認。
5. 既存出力がある場合、子プロセスを開始する前に停止し、元ファイルを保持。
6. 生成チェッカーボードで9×6の54コーナーを検出。異なる既存画像の上書きを拒否。
7. 校正と撮影のカメラserial、校正の基線長などの検査を確認。
8. キットを一時ディレクトリへコピーし、AcousTools・PyTorch・OpenEBのimportを禁止した状態で、全CLIの `--help` と `all --dry-run` が成功。
9. 切り離したコピーで人工左右イベントから2D抽出→3D復元→3D軌跡図まで実行。既知の深度600 mm、Y=0を絶対許容誤差1e-6 mmで再現し、80点を超える有効3D点を確認。同じ出力への通常再実行を拒否、同条件の中断再開で片眼追跡を再利用、条件変更後の再開を拒否。

人工イベントは10 ms分、左右各7,200イベントの小規模データです。OSの一時ディレクトリだけを使い、実測データを処理していません。

## 2026-09-16 追加修正とその確認

レビュー指摘2件を修正し、修正内容ごとに実行して確認しました。

1. `check_environment.py`: `import_metavision()` は失敗時に `SystemExit` を送出しますが、これは `BaseException` 直下で `Exception` のサブクラスではないため、従来の `except Exception` を素通りしていました。結果として `[FAIL] OpenEB:` の整形と `SETUP_OPENEB_JP.md` への誘導が表示されませんでした。`except (Exception, SystemExit)` へ変更し、Metavision不在環境で `--sdk` を実行して両方が表示されること、終了コードが1であることを確認しました。
2. `activate_metavision_env.ps1`: 期待するSDKディレクトリが1つも見つからない場合でも無条件に「activated」と表示していました。また `MV_HAL_PLUGIN_PATH` だけ `HDF5_PLUGIN_PATH` のような存在確認が無く、空文字で上書きし得ました。空文字の代入は未設定と同じではなく、システム全体に導入済みのOpenEBのプラグイン探索を妨げる可能性があります。見つからなかった項目を個別に警告し、`MV_HAL_PLUGIN_PATH` も実在時のみ設定するよう変更しました。PowerShell 7.4でパースと、(a) SDKが1つも無い場合、(b) OpenEBのみ存在する場合の2条件を実行し、(a) で `MV_HAL_PLUGIN_PATH`・`HDF5_PLUGIN_PATH` が未設定のまま・`PATH` が不変であること、(b) で両者が正しく設定されることを確認しました。

あわせて、親プロジェクトで使用中のステレオ校正を `data/session_cal20260805/calibration/` へ持ち込み、`eventcam_workflow.py` の `validate_calibration` を無改変で通過することを確認しました（基線長120.53 mm、stereo_rms 0.362 px）。出所は同ディレクトリの `IMPORTED_CALIBRATION.txt` に記載しています。

上記の変更後、`selftest.py` の9件を再実行して全件成功することを確認しました。

## 2026-09-16 追加: 単眼サンプルとEnterトリガ撮影

- カメラ1台の `mono_sample.py` / `mono_eventcam_record.py` を追加しました。撮影部分は
  `eventcam_scale_calibration_capture.py` の `capture_raw` / `export_raw_to_npz` をそのまま
  再利用し、円フィットだけを外しています。
- `stereo_eventcam_record_sync.py` に `--start-trigger {auto,enter}` を追加しました。`enter` では
  両カメラを流したままオペレータのEnterを待ち、押された時点のマスターカメラ時計から
  10スライス先にアラインした時刻を共有します。`--duration-sec 0` では2回目のEnterで
  共有の終了時刻を公開します。保存区間がマスターカメラ時計で定義・アラインされる点は
  変更していません。既定は `auto` なので、従来の呼び出しは挙動が変わりません。
  この変更により、同ファイルは親プロジェクトの無改変コピーではなくなりました
  （`SOURCE_SNAPSHOT.json` の `modified_for_standalone` を true にしています）。

実機なしで確認したこと。テストは11件から14件になり、全件成功します。

1. 合成単眼イベントからの2D抽出と動画化。抽出中心が真値と1 px以内、欠測と棄却が0、mp4生成、
   同じrunへの再実行拒否。
2. `mono_sample.py all --dry-run` に stereo・calibration・hw-sync・左右serialが現れないこと。
3. 共有区間の計算（`compute_common_interval_start` / `compute_common_interval_end`）を
   delta_t 100/1000/10000 µs、各種の現在時刻とリードで検証。スライス境界へのアライン、
   最低10スライスのリード、終了が開始より必ず後、即時の2回目Enterでも1スライス以上保存、を確認。
4. `--duration-sec 0` が `--start-trigger enter` とハード同期を要求すること、負値が拒否されること。
5. `eventcam_workflow.py` の record が `--start-trigger enter` を渡すこと、
   `record_duration_sec: 0` が設定検査を通り、負値とNaNは拒否されること。

Enterトリガ経路そのものの実機動作（実際の左右カメラでの開始・停止）は未実施です。

## 未実施の検証

- この独立版による実機の校正画像取得、同期撮影。
- 新しく取得した実画像による校正精度評価。
- クリーンな別PCへのOpenEBのビルド・ドライバ導入。
- Linux上での実機取得、スパコンのジョブ投入。

既存の校正・記録・抽出アルゴリズムを同梱していますが、上記の人工データ検証は実機の校正精度・撮影品質の代替にはなりません。実際の導入後は環境確認と短い記録から確認してください。

## 再検証コマンド

このキットのディレクトリで、そこに用意したvenvを使います。

```powershell
.\venv\Scripts\python.exe -B .\selftest.py -v
```

カメラやPATは起動しません。NumPy・OpenCV・Matplotlibが必要です。テストは一時コピー内で人工データを生成・解析します。

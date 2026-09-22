# OpenEB・Python環境の準備

対象はWindows 11 x64、SilkyEvCam HD 2台です。2026-09-16時点の作成元環境はPython 3.11.9、NumPy 1.26.4、OpenCV 4.10.0、Matplotlib 3.10.9。カメラSDKの導入実績はOpenEB 5.2.0とCenturyArksの対応プラグインです。

本キット自体のコードはこのフォルダー内で完結しますが、Python・OpenEB・ネイティブDLL・USBドライバは別途必要です。`pip install -r requirements.txt` だけではカメラを使える状態にはなりません。

この指示書は既存環境の導入記録と一次資料を基にしたものです。今回、利用先PCへのインストールや、クリーン環境での再ビルドは実行していません。

## 1. 必要範囲を選ぶ

| 用途 | インストールするもの |
|---|---|
| 保存済みNPZの粒子抽出・3D復元、保存画像の校正計算 | Python 3.11、`requirements.txt` の3ライブラリ |
| カメラから校正画像やイベントを取得 | 上記＋OpenEBのPython bindings／DLL＋SilkyEvCamドライバ／HALプラグイン |
| OpenEBをソースからビルド | 上記の開発環境＋Git、CMake、Visual Studio Build Tools、vcpkg、Python開発用ヘッダー・ライブラリ |

AcousTools、OpenMPD、PyTorch、CUDAは本キットの実行には使いません。OpenEBのMLモジュールも使用しません。

## 2. Pythonと仮想環境

Python 3.11の64-bit版をインストールします。このキットのフォルダーへ移動し、専用venvを作成します。移植しやすさとWindowsのパス長のため、例えば `C:\eventcam_standalone` のような短い配置先が便利です。

```powershell
Set-Location C:\eventcam_standalone
py -3.11 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe .\eventcam_workflow.py doctor
```

保存済みNPZの解析だけならここまでで準備できます。OpenCVはGUIを使う撮影にも対応する `opencv-python` を指定しています。`opencv-python-headless` と同時にインストールしないでください。

以下の実機環境構築は、既存環境へ上書きする前に、そのPython版・OpenEB版・ドライバ版を確認してから進めます。

## 3. OpenEBの入手と開発環境

OpenEBのWindowsソースビルド手順はWindows 11 x64を対象としています。第三者製カメラには、そのメーカーのドライバとプラグインが別途必要です。[Prophesee公式Windows手順](https://docs.prophesee.ai/stable/installation/windows_openeb.html)

この手順では、作成元で利用実績のある **OpenEB 5.2.0** を明示して取得します。「常に最新版へ更新する」という手順ではありません。Python bindingsのABI、OpenEB DLL、メーカーのプラグインの対応版を揃えてください。[OpenEB公式リポジトリ](https://github.com/prophesee-ai/openeb)

準備するもの:

- Windows 11 x64、NTFS上の作業ディレクトリ。
- Git for Windows。
- Visual Studio 2022 / Build Tools 2022のC++開発ツール、MSVC x64、Windows 11 SDK、英語Language Pack。
- CMake。5.2系の公式案内に沿って3.26系を基準とします。
- Python 3.11 x64とこのフォルダーのvenv。
- 長いパスを扱えるWindows設定。グループポリシー等で「Win32の長いパスを有効にする」を確認する。

開発ツールは公式配布元から導入します。

- [Python for Windows](https://www.python.org/downloads/windows/)
- [Visual Studio Build Tools](https://visualstudio.microsoft.com/downloads/)
- [CMake](https://cmake.org/download/)
- [Git for Windows](https://git-scm.com/downloads/win)

**Developer PowerShell for VS 2022（x64）** で、このキットのフォルダーへ移動して実行します。

```powershell
$EventcamRoot = (Get-Location).Path
git clone --branch 5.2.0 --recursive https://github.com/prophesee-ai/openeb.git openeb
git -C .\openeb submodule update --init --recursive
git clone https://github.com/microsoft/vcpkg.git vcpkg
.\vcpkg\bootstrap-vcpkg.bat
Copy-Item .\openeb\utils\windows\11\vcpkg-openeb.json .\vcpkg\vcpkg.json
Push-Location .\vcpkg
.\vcpkg.exe install --triplet x64-windows
Pop-Location
```

vcpkgの依存はOpenEBに付属するmanifestを使います。既に `openeb` や `vcpkg` がある環境では、上の取得を重複実行せず、対象バージョンと導入状態を確認します。

作成元のvcpkg配置先は `vcpkg\vcpkg_installed\x64-windows` でした。利用環境によって `vcpkg\installed\x64-windows` の場合もあります。

```powershell
Test-Path .\vcpkg\vcpkg_installed\x64-windows\bin
Test-Path .\vcpkg\installed\x64-windows\bin
```

以降は、実際にライブラリが入った方を使います。

## 4. Python bindingのビルド・インストール

本キットが必要とするOpenEBはHAL、Base、Core、Stream、UIです。NPZ後処理の依存とOpenEBビルドの依存は別です。まずビルド用のPythonパッケージを同じvenvへ追加します。

```powershell
.\venv\Scripts\python.exe -m pip install pybind11 h5py pandas "cmake==3.26.4" wheel setuptools
$env:PATH = "$EventcamRoot\venv\Scripts;$env:PATH"
$env:PYTHONNOUSERSITE = "true"
$EventcamPythonBase = & .\venv\Scripts\python.exe -c "import sys; print(sys.base_prefix)"
$EventcamVcpkgInstalled = "$EventcamRoot\vcpkg\vcpkg_installed"
```

`vcpkg\installed`を使っている場合は、最後の変数だけそのパスへ変更します。

```powershell
cmake -S .\openeb -B .\openeb_build -G "Visual Studio 17 2022" -A x64 `
  -DCMAKE_BUILD_TYPE=Release `
  -DCMAKE_TOOLCHAIN_FILE="$EventcamRoot\openeb\cmake\toolchains\vcpkg.cmake" `
  -DCMAKE_INSTALL_PREFIX="$EventcamRoot\openeb_install" `
  -DVCPKG_DIRECTORY="$EventcamRoot\vcpkg" `
  -DVCPKG_TARGET_TRIPLET=x64-windows `
  -DVCPKG_INSTALLED_DIR="$EventcamVcpkgInstalled" `
  -DCMAKE_PREFIX_PATH="$EventcamVcpkgInstalled\x64-windows" `
  -DMETAVISION_SELECTED_MODULES="base;core;stream;ui" `
  -DCOMPILE_PYTHON3_BINDINGS=ON `
  -DPYTHON3_SITE_PACKAGES="$EventcamRoot\venv\Lib\site-packages" `
  -DPython3_EXECUTABLE="$EventcamRoot\venv\Scripts\python.exe" `
  -DPython3_INCLUDE_DIR="$EventcamPythonBase\Include" `
  -DPython3_LIBRARY="$EventcamPythonBase\libs\python311.lib" `
  -DBUILD_SAMPLES=OFF -DBUILD_TESTING=OFF

cmake --build .\openeb_build --config Release --parallel 4
cmake --build .\openeb_build --config Release --target install
```

Pythonのinclude/libはvenvではなく、元のPython本体を参照します。DLLやbindingのインストール先はこのキット内へ統一します。OpenEBの公式全機能用requirementsには、このキットが使わないモジュールや異なるOpenCV指定も含まれ得るため、既存venvに無条件で一括適用せず、選択モジュールのCMake診断に従って不足分を確認してください。

インストール後の存在確認:

```powershell
Test-Path .\openeb_install\bin\metavision_hal.dll
Test-Path .\venv\Lib\site-packages\metavision_core
Test-Path .\venv\Lib\site-packages\metavision_sdk_ui
```

このローカルビルドではbindingsをvenvへ直接入れるため、`--system-site-packages` は使っていません。既存のシステムインストールを使う場合は、そのbindingsが対象PythonのABIに対応し、対象venvから見えるように別途設定します。

## 5. SilkyEvCamのドライバ・プラグイン

CenturyArks公式ダウンロードページで、カメラ型式・OS・SDK版に対応するパッケージを取得し、付属readmeの手順で導入してください。確認時点ではWindows 11向けの `SilkyEvCam_Plugin_Installer_for_win64_v5.2.0.zip` がV5.2/V5.3用として掲載されています。[CenturyArks公式ダウンロード](https://centuryarks.com/download-2/)

OpenEB標準のPropheseeプラグインだけではSilkyEvCamを使えるとは限りません。メーカーのUSBドライバとHALプラグインの両方を確認します。メーカーのインストーラーをGUIで実行し、再起動が案内されたら従います。このキットが自動でドライバやファームウェアを更新することはありません。

作成元環境の代表的な配置:

```text
C:\Program Files\CenturyArks\bin\
C:\Program Files\CenturyArks\plugins\silky_common_plugin.dll
```

Windowsのデバイスマネージャーでカメラが正常認識され、エラーコードがないことも確認してください。

## 6. 環境変数とSDK読込みの確認

同梱の有効化スクリプトは、このキット内部のSDKとvcpkg、メーカーの標準導入先を設定します。既存の環境変数は維持して追加します。

```powershell
. .\activate_metavision_env.ps1
.\venv\Scripts\python.exe .\eventcam_workflow.py doctor --sdk
```

PowerShellがスクリプト実行を拒否する場合は、組織の方針に従い、このセッションだけ許可する方法を確認してください。例:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

SDKを独自の場所へ導入した場合は、次の環境変数で明示できます。親プロジェクトのパスを自動探索することはありません。

| 変数 | 指定内容 |
|---|---|
| `EVENTCAM_OPENEB_ROOT` | OpenEBのインストールルート。内部にbin/libがある場所 |
| `EVENTCAM_VCPKG_BIN` | vcpkgの依存DLLがあるbinディレクトリ |
| `EVENTCAM_CAMERA_BIN_DIR` | カメラメーカーのDLLディレクトリ |
| `EVENTCAM_CAMERA_PLUGIN_DIR` | カメラメーカーのHALプラグインディレクトリ |

既存SDKを使う場合の例:

```powershell
$env:EVENTCAM_OPENEB_ROOT = "C:\SDK\openeb_install"
$env:EVENTCAM_VCPKG_BIN = "C:\SDK\vcpkg\vcpkg_installed\x64-windows\bin"
. .\activate_metavision_env.ps1
.\venv\Scripts\python.exe .\eventcam_workflow.py doctor --sdk
```

これだけではPython bindingsの検索先は変わりません。bindingsは使っているvenvへインストールするか、適合する導入先を明示的に設定してください。異なるPython版の `.pyd` をコピーして使わないでください。

`doctor --sdk` はimportまでです。カメラの列挙は明示的に次を実行します。

```powershell
.\venv\Scripts\python.exe .\eventcam_workflow.py devices
```

`00000508` と `00000509` が見えることを確認します。列挙の成功だけでは同期ケーブルやイベント取得は検証できません。実際のプレビューと記録は [README_JP.md](README_JP.md) に従います。

## 7. 作成元環境で遭遇した問題

以下は必ず行う変更ではありません。同じ症状が発生した場合だけ、対象SDK版・ビルドログを確認して適用を検討します。元ファイルをバックアップし、変更を記録してください。

### `No module named metavision_core`

対象venvへbindingsがインストールされているかを確認します。`doctor`のPythonパスが意図したものか、`PYTHON3_SITE_PACKAGES`が別環境を指していなかったかを確認します。

### DLL load failed / `[HAL] no plugin found`

Python、OpenEB、プラグインがすべてx64で対応版か確認します。`MV_HAL_PLUGIN_PATH`にメーカーのpluginsが含まれる必要があります。DLLの探索先としてOpenEBのbin、vcpkgのbin、メーカーのbin/pluginsが必要です。同梱のSDK共通処理は、Metavisionをimportする直前に `os.add_dll_directory()` で既存ディレクトリだけを登録します。

### カメラ一覧が空

メーカーのドライバ、デバイスマネージャー、USBケーブルと接続先、プラグインの対応版を確認します。GUI viewerや別Pythonプロセスがカメラを占有している場合は終了してください。Pythonパッケージを追加するだけでUSBドライバの問題は解決しません。

### HDF5 DLL衝突

作成元では、pipの `h5py` が持つHDF5 DLLとOpenEB/vcpkgのHDF5 DLLが競合しました。RAW/NPZを使う処理でも、Metavisionが `h5_io.py` を早期importすると影響し得ます。

- `.pth` や `sitecustomize.py` で全Python実行へvcpkgのDLL探索先を強制追加しない。
- 同じvenv・同じSDK版で、`doctor --sdk` の失敗箇所を確認する。
- 作成元では `metavision_core/event_io/h5_io.py` のトップレベル `import h5py` を、実際にHDF5を開く関数内へ遅延する局所修正を用いた。これを行う場合、関数本体だけでなく型注釈などの早期参照も確認する。
- SDK再インストールで局所修正が戻ることがある。修正内容を記録しておく。

このキットはSDKファイルを自動修正しません。HDF5ファイルを使わないことと、HDF5 DLL依存が一切ないことは同じではありません。

### `Python3::Python_3.11-NOTFOUND` のリンクエラー

Python本体のinclude/libと `Python3_EXECUTABLE` の組合せを確認します。作成元ではOpenEBの `cmake/custom_functions/python3.cmake` で、Windows側を `Python3_LIBRARY` へ明示リンクし、`Python3_INCLUDE_DIRS` を明示する局所対処が必要でした。CMakeが正しくPythonを検出する環境では変更しません。

### HDF5 pluginの `create_symlink` 失敗

NTFS、Developer Modeまたは必要な権限を確認します。作成元では `sdk/modules/stream/cpp/3rdparty/hdf5_ecf/CMakeLists.txt` の該当リンク作成を `cmake -E copy_if_different` に置き換える対処を用いました。出力先と対象DLLが正しいことを確認してから再installします。

### Boostの `Unsupported MSVC toolset version`

作成元では新しいMSVCとBoost 1.84の判定不一致がありました。まずOpenEBが想定するVisual Studio/MSVCとvcpkg manifestを揃えます。特定版で必要となったBoost Build側のtoolset判定修正を、別版へ無条件で流用しないでください。

## 8. 利用開始前の確認

1. `doctor` が成功する。
2. 実機を使う環境では `doctor --sdk` と `devices` が成功する。
3. `all --dry-run` のserial、出力先、7.12 mm、左Master同期を確認する。
4. [README_JP.md](README_JP.md) に沿って盤面の実寸・カメラ固定・同期配線を確認する。
5. 最初の短い撮影で保存成功を確認し、抽出・3D結果を見てから撮影時間や件数を増やす。

Linuxで撮影も行う場合は、そのOS向けのOpenEBとメーカーのプラグインを別途導入してください。このキットの移植テストはWindows上の切り離したコピーで行っており、Linux実機取得の動作を今回保証するものではありません。NPZのオフライン解析手順はREADMEに記載しています。

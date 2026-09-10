# Windows Event Camera Setup Tutorial v2

この文書は、`WINDOWS_EVENTCAM_SETUP.md` の第2版です。

今回、実際に Windows PC 上の既存 `venv` へ OpenEB / Metavision Python bindings を導入し、SilkyEvCam HD を `eventcam_enter_capture.py --list-devices` で認識させるまでに必要だった追加事項を反映しています。

特に、元の手順だけでは見落としやすかった次を詳しく書いています。

- vcpkg の実際の DLL 出力先が `vcpkg\installed` ではなく `vcpkg\vcpkg_installed` になる場合
- Visual Studio 2022 の新しい MSVC と Boost 1.84 の判定問題
- Python 3.11 binding ビルド時の include / lib / link 設定
- OpenEB install 時の HDF5 symlink エラー
- `h5py` と Metavision stream binding の HDF5 DLL 衝突
- SilkyEvCam HD 用の CenturyArks driver / HAL plugin の導入
- `Detected devices: none` の切り分け

## 0. 今回成功した構成

|項目|値|
|---|---|
|OS|Windows 11 x64|
|Python|Python 3.11 x64|
|仮想環境|既存 `venv`|
|OpenEB|5.2.0|
|SilkyEvCam plugin|CenturyArks v5.2.0|
|OpenEB install先|`openeb_install`|
|vcpkg依存先|`vcpkg\vcpkg_installed\x64-windows`|
|確認カメラ|SilkyEvCam HD v03.06.00C|

以下では、作業フォルダを `$Workspace` と呼びます。

このリポジトリでは例として次です。

```powershell
$Workspace = "C:\Users\digit\Documents\scripts\python\eventcam_control"
```

## 1. venv を有効化する

既存 venv を使います。

```powershell
Set-Location $Workspace
.\venv\Scripts\Activate.ps1
```

PowerShell の実行ポリシーで止まる場合:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\venv\Scripts\Activate.ps1
```

基本パッケージを入れます。

```powershell
python -m pip install --upgrade pip setuptools wheel
python -m pip install numpy==1.26.4 opencv-python==4.10.0.84 h5py matplotlib pandas pybind11 cmake
```

注意:

`torch` が入っている環境では、`setuptools<82` などの依存警告が出ることがあります。Metavision binding のビルド自体は進みますが、後で PyTorch 側に問題が出る場合は `setuptools` のバージョンを調整してください。

確認:

```powershell
python -c "import sys, numpy, cv2; print(sys.executable); print(numpy.__version__); print(cv2.__version__)"
```

## 2. OpenEB 5.2.0 を取得する

SilkyEvCam plugin v5.2.0 と合わせるため、OpenEB も 5.2.0 を使います。

```powershell
git clone --branch 5.2.0 --recursive https://github.com/prophesee-ai/openeb.git openeb
git -C .\openeb submodule update --init --recursive
```

## 3. vcpkg を準備する

```powershell
git clone https://github.com/microsoft/vcpkg.git vcpkg
.\vcpkg\bootstrap-vcpkg.bat
Copy-Item .\openeb\utils\windows\11\vcpkg-openeb.json .\vcpkg\vcpkg.json
.\vcpkg\vcpkg.exe install --triplet x64-windows
```

### 3.1 vcpkg の出力先に注意

元手順では次を想定していました。

```text
vcpkg\installed\x64-windows
```

今回の環境では実際には次へ入りました。

```text
vcpkg\vcpkg_installed\x64-windows
```

以降の `PATH`、`CMAKE_PREFIX_PATH`、スクリプト内 DLL 探索では、実在する方を使ってください。

確認:

```powershell
Test-Path .\vcpkg\vcpkg_installed\x64-windows\bin
Test-Path .\vcpkg\installed\x64-windows\bin
```

## 4. Boost 1.84 と新しい MSVC の問題

Visual Studio 2022 の MSVC が 19.50 以降の場合、vcpkg の Boost 1.84 側で toolset 判定に失敗することがあります。

症状の例:

```text
Unsupported MSVC toolset version
```

今回の回避では、vcpkg 側の Boost Build CMake 設定を修正し、MSVC 19.50 以降も toolset 143 として扱いました。

対象ファイル:

```text
vcpkg\vcpkg_installed\x64-windows\share\boost-build\CMakeLists.txt
```

方針:

```cmake
# MSVC 19.50 以降も Visual Studio 2022 v143 toolset として扱う
```

この問題は vcpkg / Boost / MSVC の組み合わせで変わるため、常に必要とは限りません。vcpkg install が成功している場合は触らなくて大丈夫です。

## 5. OpenEB configure

Developer PowerShell for VS 2022、または x64 Native Tools Command Prompt for VS 2022 で実行します。

```powershell
Set-Location $Workspace
.\venv\Scripts\Activate.ps1
```

Python include / lib の場所を確認します。

```powershell
python -c "import sysconfig; print(sysconfig.get_path('include')); print(sysconfig.get_config_var('LIBDIR'))"
```

典型例:

```text
C:\Users\<user>\AppData\Local\Programs\Python\Python311\Include
C:\Users\<user>\AppData\Local\Programs\Python\Python311\libs
```

configure 例:

```powershell
cmake -S .\openeb -B .\openeb_build -A x64 `
  -DCMAKE_BUILD_TYPE=Release `
  -DCMAKE_TOOLCHAIN_FILE="$Workspace\openeb\cmake\toolchains\vcpkg.cmake" `
  -DCMAKE_INSTALL_PREFIX="$Workspace\openeb_install" `
  -DVCPKG_DIRECTORY="$Workspace\vcpkg" `
  -DVCPKG_TARGET_TRIPLET=x64-windows `
  -DVCPKG_INSTALLED_DIR="$Workspace\vcpkg\vcpkg_installed" `
  -DCMAKE_PREFIX_PATH="$Workspace\vcpkg\vcpkg_installed\x64-windows" `
  -DMETAVISION_SELECTED_MODULES="base;core;stream;ui" `
  -DCOMPILE_PYTHON3_BINDINGS=ON `
  -DPYTHON3_SITE_PACKAGES="$Workspace\venv\Lib\site-packages" `
  -DPython3_EXECUTABLE="$Workspace\venv\Scripts\python.exe" `
  -DPython3_INCLUDE_DIR="C:\Users\<user>\AppData\Local\Programs\Python\Python311\Include" `
  -DPython3_LIBRARY="C:\Users\<user>\AppData\Local\Programs\Python\Python311\libs\python311.lib" `
  -DBUILD_SAMPLES=OFF `
  -DBUILD_TESTING=OFF
```

`<user>` は自分の Windows ユーザー名に置き換えてください。

重要:

- `VCPKG_INSTALLED_DIR` は今回 `vcpkg\vcpkg_installed` を明示しました。
- `Python3_INCLUDE_DIR` と `Python3_LIBRARY` は venv ではなく、venv の元になった Python 本体の include / lib を指します。
- `BUILD_SAMPLES=OFF` にすると、サンプルアプリ周りで詰まりにくくなります。

## 6. Python binding の link エラー対策

今回、Python binding の Visual Studio project が次のような未解決リンク名を持つことがありました。

```text
Python3::Python_3.11-NOTFOUND
```

その場合、OpenEB の CMake helper を修正します。

対象:

```text
openeb\cmake\custom_functions\python3.cmake
```

対応方針:

- `add_library(...)` 後に Python include を明示する
- Windows では `Python3::Python_${python_version}` ではなく `${Python3_LIBRARY}` に link する

概念的には次のような修正です。

```cmake
target_include_directories(${target_name} PRIVATE ${Python3_INCLUDE_DIRS})
target_link_libraries(${target_name} PRIVATE ${Python3_LIBRARY})
```

この修正は OpenEB / CMake / Python 検出状態によって不要な場合があります。configure と build が通るなら触らなくて構いません。

## 7. HDF5 symlink エラー対策

Windows の権限や Developer Mode の状態によって、install 時に `create_symlink` が失敗することがあります。

対象:

```text
openeb\sdk\modules\stream\cpp\3rdparty\hdf5_ecf\CMakeLists.txt
```

`create_symlink` の代わりに `copy_if_different` を使います。

```cmake
execute_process(COMMAND ${CMAKE_COMMAND} -E copy_if_different "$<TARGET_FILE:hdf5_ecf_codec>" "${HDF5_ECF_PLUGIN_INSTALL_PATH}/$<TARGET_FILE_NAME:hdf5_ecf_codec>")
```

## 8. build / install

```powershell
cmake --build .\openeb_build --config Release --parallel 4
cmake --build .\openeb_build --config Release --target install
```

確認:

```powershell
Test-Path .\openeb_install\bin\metavision_hal.dll
Test-Path .\openeb_install\lib\metavision\hal\plugins\hal_plugin_prophesee.dll
Test-Path .\venv\Lib\site-packages\metavision_core
Test-Path .\venv\Lib\site-packages\metavision_sdk_ui
```

## 9. HDF5 DLL 衝突への注意

今回の環境では、`h5py` が持つ HDF5 DLL と、OpenEB / vcpkg が使う HDF5 DLL が衝突しました。

症状:

- `h5py` を先に import すると `metavision_sdk_stream` が失敗する
- vcpkg の DLL dir を Python 全体へ早すぎる段階で追加すると `h5py` が失敗する

回避方針:

- `sitecustomize.py` や `.pth` で vcpkg DLL path を全 Python import に対して強制追加しない
- Metavision stream binding を import する直前に、必要な DLL dir だけ `os.add_dll_directory()` で足す
- `metavision_core.event_io.h5_io` の `h5py` import は、HDF5 ファイルを読む時まで遅延させる

今回の実作業では、venv 内の `metavision_core\event_io\h5_io.py` で `h5py` を lazy import にしました。

概念例:

```python
def _get_h5py():
    import h5py
    return h5py
```

そして `HDF5EventsReader.__init__` など、実際に HDF5 を開く場所で `h5py = _get_h5py()` とします。

この修正は venv 内の installed package に対する局所パッチです。OpenEB を再 install すると戻る可能性があります。

## 10. activation script

今回の実環境では、`activate_metavision_env.ps1` は次の形にしました。

```powershell
$Workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$Workspace\venv\Scripts\Activate.ps1"
$CenturyArks = "$env:ProgramFiles\CenturyArks"
$env:PATH = "$Workspace\openeb_install\bin;$Workspace\vcpkg\vcpkg_installed\x64-windows\bin;$Workspace\openeb_install\lib\metavision\hal\plugins;$CenturyArks\bin;$CenturyArks\plugins;$env:PATH"
$env:MV_HAL_PLUGIN_PATH = "$Workspace\openeb_install\lib\metavision\hal\plugins;$CenturyArks\plugins"
$env:HDF5_PLUGIN_PATH = "$Workspace\openeb_install\lib\hdf5\plugin"
$env:PYTHONNOUSERSITE = "true"
```

ポイント:

- `vcpkg\installed` ではなく `vcpkg\vcpkg_installed` を使う
- CenturyArks installer が入れる `C:\Program Files\CenturyArks\bin` と `plugins` を追加する
- `MV_HAL_PLUGIN_PATH` に CenturyArks plugin path を追加する

## 11. SilkyEvCam driver / plugin を入れる

SilkyEvCam HD は OpenEB 標準の Prophesee plugin だけでは認識しません。

CenturyArks のダウンロードページから、Metavision / OpenEB のバージョンに合う installer を取得します。

今回使用:

```text
SilkyEvCam_Plugin_Installer_for_win64_v5.2.0.zip
SilkyEvCam_Installer_for_win64_v5.2.0.exe
```

readme の要点:

- Metavision 5.2.0 用
- SilkyEvCam HD / HD-Lite / Gen31 対応
- installer 実行後、Windows 再起動を推奨

実行例:

```powershell
Start-Process -Verb RunAs -FilePath ".\SilkyEvCam_Plugin_Installer_for_win64_v5.2.0\SilkyEvCam_Plugin_Installer_for_win64_v5.2.0\SilkyEvCam_Installer_for_win64_v5.2.0.exe"
```

今回、silent install のつもりで `/S` を試すと installer が対話待ちで残りました。GUI installer として進める方が確実です。

インストール後にできる代表的なファイル:

```text
C:\Program Files\CenturyArks\bin\metavision_viewer.exe
C:\Program Files\CenturyArks\bin\metavision_hal.dll
C:\Program Files\CenturyArks\bin\libusb-1.0.dll
C:\Program Files\CenturyArks\plugins\silky_common_plugin.dll
```

## 12. Windows 側の driver 状態を確認する

`Detected devices: none` の時は、まず Python ではなく Windows PnP を見ます。

```powershell
Get-PnpDevice -PresentOnly |
  Where-Object { $_.FriendlyName -match "Silky|Prophesee|Metavision|Event" } |
  Select-Object Status,Class,FriendlyName,InstanceId |
  Format-Table -AutoSize
```

今回、installer 前は次の状態でした。

```text
Status FriendlyName
------ ------------
Error  SilkyEvCam HD v03.06.00C
```

詳細確認:

```powershell
$id = (Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -match "SilkyEvCam" }).InstanceId
Get-PnpDeviceProperty -InstanceId $id -KeyName `
  'DEVPKEY_Device_ProblemCode',`
  'DEVPKEY_Device_ProblemStatus',`
  'DEVPKEY_Device_DriverProvider',`
  'DEVPKEY_Device_DriverVersion',`
  'DEVPKEY_Device_DriverInfPath',`
  'DEVPKEY_Device_Service',`
  'DEVPKEY_Device_Class' |
  Select-Object KeyName,Data |
  Format-List
```

installer 前:

```text
DEVPKEY_Device_ProblemCode    28
DEVPKEY_Device_DriverProvider
DEVPKEY_Device_Service
```

`ProblemCode: 28` はドライバ未インストールです。

installer 後:

```text
DEVPKEY_Device_ProblemCode    0
DEVPKEY_Device_DriverProvider libwdi
DEVPKEY_Device_Service        WinUSB
DEVPKEY_Device_DriverInfPath  oem202.inf
DEVPKEY_Device_Class          USBDevice
```

この状態になれば Windows 側の認識は OK です。

## 13. Python から import と device discovery を確認する

```powershell
.\activate_metavision_env.ps1
python -c "from metavision_core.event_io import EventsIterator; from metavision_core.event_io.raw_reader import initiate_device; import metavision_hal; print('imports OK'); print(metavision_hal.DeviceDiscovery.list())"
```

成功例:

```text
imports OK
['CenturyArks:silky_common_plugin:00000064']
```

このリポジトリの撮影スクリプトでも確認します。

```powershell
python eventcam_enter_capture.py --list-devices
```

成功例:

```text
Detected devices:
  CenturyArks:silky_common_plugin:00000064
```

## 14. `eventcam_enter_capture.py` 側の追加対応

今回、スクリプト内の自己設定でも Metavision DLL / plugin を見つけられるようにしました。

重要な点:

- `vcpkg\vcpkg_installed\x64-windows\bin` を優先
- なければ `vcpkg\installed\x64-windows\bin` に fallback
- `C:\Program Files\CenturyArks\bin` を `PATH` / DLL directory に追加
- `C:\Program Files\CenturyArks\plugins` を `MV_HAL_PLUGIN_PATH` / DLL directory に追加

概念:

```python
vcpkg_bin_dir = project_dir / "vcpkg" / "vcpkg_installed" / "x64-windows" / "bin"
if not vcpkg_bin_dir.exists():
    vcpkg_bin_dir = project_dir / "vcpkg" / "installed" / "x64-windows" / "bin"

centuryarks_dir = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "CenturyArks"
centuryarks_bin_dir = centuryarks_dir / "bin"
centuryarks_plugin_dir = centuryarks_dir / "plugins"
```

これにより、venv の Python を直接指定しても動きます。

```powershell
.\venv\Scripts\python.exe eventcam_enter_capture.py --list-devices
```

## 15. よくある症状と今回の答え

### `No module named 'metavision_core'`

venv ではない Python を使っています。

確認:

```powershell
python -c "import sys; print(sys.executable)"
```

venv の Python を直指定:

```powershell
.\venv\Scripts\python.exe eventcam_enter_capture.py --list-devices
```

### `Metavision Python/UI bindings are not available`

可能性:

- venv に OpenEB Python bindings が入っていない
- `openeb_install\bin` が DLL 探索に入っていない
- vcpkg DLL path が違う
- `metavision_sdk_ui` の DLL 依存が見つからない

まず:

```powershell
.\activate_metavision_env.ps1
python -c "import metavision_hal, metavision_sdk_ui, metavision_sdk_stream; print('ok')"
```

### `Detected devices: none`

今回の原因は次でした。

1. SilkyEvCam driver が未インストール
2. CenturyArks の `silky_common_plugin.dll` が `MV_HAL_PLUGIN_PATH` に入っていない

確認順:

```powershell
Get-PnpDevice -PresentOnly |
  Where-Object { $_.FriendlyName -match "Silky|Prophesee|Metavision|Event" } |
  Select-Object Status,Class,FriendlyName,InstanceId |
  Format-Table -AutoSize

$env:MV_HAL_PLUGIN_PATH
Test-Path "C:\Program Files\CenturyArks\plugins\silky_common_plugin.dll"
python -c "import metavision_hal; print(metavision_hal.DeviceDiscovery.list())"
```

### `[HAL][WARNING] no plugin found`

`MV_HAL_PLUGIN_PATH` が不足しています。

SilkyEvCam では少なくとも次を含めます。

```text
<workspace>\openeb_install\lib\metavision\hal\plugins
C:\Program Files\CenturyArks\plugins
```

### `h5py` または `metavision_sdk_stream` の HDF5 DLL エラー

DLL の読み込み順序問題です。

避けること:

- vcpkg DLL path を `.pth` や `sitecustomize.py` で全 Python 実行へ常時追加する

対処:

- Metavision import 直前に `os.add_dll_directory()` する
- `h5py` は必要になるまで import を遅らせる

## 16. 最終チェックリスト

```powershell
Set-Location $Workspace
.\activate_metavision_env.ps1

python -c "import sys; print(sys.executable)"
python -c "import metavision_hal, metavision_sdk_ui, metavision_sdk_stream; print('imports OK')"
python -c "import metavision_hal; print(metavision_hal.DeviceDiscovery.list())"
python eventcam_enter_capture.py --list-devices
```

期待結果:

```text
imports OK
['CenturyArks:silky_common_plugin:00000064']
Detected devices:
  CenturyArks:silky_common_plugin:00000064
```

ここまで通れば、撮影スクリプトを起動できます。

```powershell
python eventcam_enter_capture.py
```

撮影後の RAW 後処理:

```powershell
python eventcam_raw_postprocess.py .\recordings_eventcam\<recording>.json --fps 1000 --video-fps 50
```

## 17. 今回作成・変更した主なファイル

```text
activate_metavision_env.ps1
eventcam_enter_capture.py
openeb
openeb_build
openeb_install
vcpkg
SilkyEvCam_Plugin_Installer_for_win64_v5.2.0.zip
SilkyEvCam_Plugin_Installer_for_win64_v5.2.0
```

`openeb_build` と vcpkg はサイズが大きくなります。別 PC へ移す場合は、通常は再ビルドする方が安全です。


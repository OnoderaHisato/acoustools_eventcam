# Windows Event Camera Setup Tutorial

このチュートリアルは、Windows で Prophesee OpenEB / Metavision Python SDK をビルドし、SilkyEvCam などのイベントカメラを Python から撮影できるようにするための汎用手順です。ここではイベントカメラの認識、RAW 撮影、RAW 後処理までを扱います。

## 想定構成

|項目|例|
|---|---|
|OS|Windows 10/11 x64|
|Python|Python 3.11 x64|
|仮想環境|`venv`|
|OpenEB|v5.2.0|
|OpenEB install先|`openeb_install`|
|カメラ例|SilkyEvCam HD|

この文書では、任意の作業フォルダを `$Workspace` として表します。例として `D:\eventcam_workspace` や `%USERPROFILE%\Documents\eventcam_workspace` など、好きな場所を使ってください。

参考リンク:

- Prophesee OpenEB: https://github.com/prophesee-ai/openeb
- OpenEB Windows installation: https://docs.prophesee.ai/stable/installation/windows_openeb.html
- Metavision Python get started: https://docs.prophesee.ai/stable/get_started/get_started_python.html
- Metavision Python API: https://docs.prophesee.ai/stable/api/python/index.html
- CenturyArks download page: https://centuryarks.com/en/download/

## 1. 事前に入れるもの

以下をインストールしておきます。

- Git for Windows
- Python 3.11 x64
- Visual Studio 2022 Build Tools
- CMake
- VS Code 任意
- VS Code Python extension 任意

Visual Studio 2022 Build Tools では、少なくとも次を入れてください。

- Desktop development with C++
- MSVC v143
- Windows 10/11 SDK
- C++ CMake tools for Windows

Python は python.org の 64-bit Python 3.11 を推奨します。Microsoft Store 版でも動く場合がありますが、ビルド時の include/lib パス確認が分かりにくくなることがあります。

## 2. 作業フォルダを決める

PowerShell を開き、作業フォルダを作ります。以下は例です。

```powershell
$Workspace = "$env:USERPROFILE\Documents\eventcam_workspace"
New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
Set-Location $Workspace
```

以降、すべてのコマンドは `$Workspace` で実行する想定です。

## 3. Python venv を作る

```powershell
py -3.11 -m venv venv
```

PowerShell の実行ポリシーで `Activate.ps1` が止まる場合は、現在の PowerShell だけ許可します。

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

venv を有効化します。

```powershell
.\venv\Scripts\Activate.ps1
```

pip と基本パッケージを入れます。

```powershell
python -m pip install --upgrade pip setuptools wheel
python -m pip install numpy==1.26.4 opencv-python==4.10.0.84 h5py matplotlib pandas pybind11
```

確認:

```powershell
python -c "import sys, numpy, cv2; print(sys.executable); print(numpy.__version__); print(cv2.__version__)"
```

## 4. OpenEB を取得する

SilkyEvCam Plugin v5.2.0 を使う場合は、OpenEB も v5.2.0 に合わせます。

```powershell
git clone --branch 5.2.0 --recursive https://github.com/prophesee-ai/openeb.git openeb
```

サブモジュールが不足している場合:

```powershell
git -C .\openeb submodule update --init --recursive
```

## 5. vcpkg を準備する

OpenEB の依存ライブラリ用に vcpkg を用意します。

```powershell
git clone https://github.com/microsoft/vcpkg.git vcpkg
.\vcpkg\bootstrap-vcpkg.bat
```

OpenEB には Windows 用の vcpkg manifest が同梱されています。Windows 11 用 manifest の例:

```powershell
Copy-Item .\openeb\utils\windows\11\vcpkg-openeb.json .\vcpkg\vcpkg.json
.\vcpkg\vcpkg.exe install --triplet x64-windows
```

Windows 10 で使う場合や OpenEB のディレクトリ構成が変わっている場合は、`openeb\utils\windows` 以下にある該当 manifest を確認してください。

## 6. OpenEB をビルドする

Visual Studio のコンパイラが見える PowerShell で実行します。簡単なのは、スタートメニューから次のどちらかを開く方法です。

- x64 Native Tools Command Prompt for VS 2022
- Developer PowerShell for VS 2022

作業フォルダへ移動し、venv を有効化します。

```powershell
Set-Location $Workspace
.\venv\Scripts\Activate.ps1
```

CMake configure:

```powershell
cmake -S .\openeb -B .\openeb_build -A x64 `
  -DCMAKE_BUILD_TYPE=Release `
  -DCMAKE_TOOLCHAIN_FILE="$Workspace\openeb\cmake\toolchains\vcpkg.cmake" `
  -DCMAKE_INSTALL_PREFIX="$Workspace\openeb_install" `
  -DPYTHON3_SITE_PACKAGES="$Workspace\venv\Lib\site-packages" `
  -DVCPKG_DIRECTORY="$Workspace\vcpkg" `
  -DVCPKG_TARGET_TRIPLET=x64-windows `
  -DMETAVISION_SELECTED_MODULES="base;core;stream;ui" `
  -DCOMPILE_PYTHON3_BINDINGS=ON `
  -DBUILD_TESTING=OFF
```

Build:

```powershell
cmake --build .\openeb_build --config Release --parallel 4
```

Install:

```powershell
cmake --build .\openeb_build --config Release --target install
```

インストール後、主に次ができていることを確認します。

```text
openeb_install\bin
openeb_install\lib\metavision\hal\plugins
venv\Lib\site-packages\metavision_hal
venv\Lib\site-packages\metavision_core
venv\Lib\site-packages\metavision_sdk_core
venv\Lib\site-packages\metavision_sdk_stream
venv\Lib\site-packages\metavision_sdk_ui
```

### hdf5_ecf の symlink エラー対策

Windows の権限設定によっては、OpenEB install 時に `create_symlink` で失敗することがあります。

その場合は、次のファイルを開きます。

```text
openeb\sdk\modules\stream\cpp\3rdparty\hdf5_ecf\CMakeLists.txt
```

Windows 用 install 処理の `create_symlink` を `copy_if_different` に変更します。

```cmake
execute_process(COMMAND ${CMAKE_COMMAND} -E copy_if_different "$<TARGET_FILE:hdf5_ecf_codec>" "${HDF5_ECF_PLUGIN_INSTALL_PATH}/$<TARGET_FILE_NAME:hdf5_ecf_codec>")
```

その後、もう一度 install します。

```powershell
cmake --build .\openeb_build --config Release --target install
```

## 7. Metavision 用の環境変数を設定する

OpenEB の DLL と HAL plugin を Python から見つけるため、PowerShell セッションごとに以下を設定します。

```powershell
.\venv\Scripts\Activate.ps1

$env:PATH = "$Workspace\openeb_install\bin;$Workspace\vcpkg\installed\x64-windows\bin;$Workspace\openeb_install\lib\metavision\hal\plugins;$env:PATH"
$env:MV_HAL_PLUGIN_PATH = "$Workspace\openeb_install\lib\metavision\hal\plugins"
$env:HDF5_PLUGIN_PATH = "$Workspace\openeb_install\lib\hdf5\plugin"
$env:PYTHONNOUSERSITE = "true"
```

毎回入力したくない場合は、作業フォルダに `activate_metavision_env.ps1` などを作り、上記を入れておくと楽です。

例:

```powershell
@'
$Workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$Workspace\venv\Scripts\Activate.ps1"
$env:PATH = "$Workspace\openeb_install\bin;$Workspace\vcpkg\installed\x64-windows\bin;$Workspace\openeb_install\lib\metavision\hal\plugins;$env:PATH"
$env:MV_HAL_PLUGIN_PATH = "$Workspace\openeb_install\lib\metavision\hal\plugins"
$env:HDF5_PLUGIN_PATH = "$Workspace\openeb_install\lib\hdf5\plugin"
$env:PYTHONNOUSERSITE = "true"
'@ | Set-Content -Encoding UTF8 .\activate_metavision_env.ps1
```

確認:

```powershell
python -c "from metavision_core.event_io import EventsIterator; from metavision_core.event_io.raw_reader import initiate_device; import metavision_hal; print('Metavision imports OK')"
```

## 8. SilkyEvCam plugin / driver を入れる

SilkyEvCam HD は OpenEB 標準の Prophesee plugin だけでは認識できません。CenturyArks の SilkyEvCam 用 plugin / driver を入れます。

CenturyArks のダウンロードページから、OpenEB/Metavision のバージョンに合う SilkyEvCam Plugin Installer を取得してください。

例:

```text
SilkyEvCam_Plugin_Installer_for_win64_v5.2.0.zip
```

展開後、管理者権限でインストーラを実行します。

```powershell
Start-Process -Verb RunAs -FilePath ".\vendor_downloads\SilkyEvCam_Plugin_Installer_for_win64_v5.2.0\SilkyEvCam_Plugin_Installer_for_win64_v5.2.0\SilkyEvCam_Installer_for_win64_v5.2.0.exe"
```

インストール後、Windows を再起動します。

再起動後、CenturyArks の `Metavision Viewer` でカメラが見えるか確認します。

## 9. デバイス認識を確認する

venv と環境変数を有効化します。

```powershell
.\activate_metavision_env.ps1
```

Python からデバイス一覧を確認します。

```powershell
python -c "import metavision_hal; print(metavision_hal.DeviceDiscovery.list())"
```

成功すると空リストではなく、カメラ情報が表示されます。

例:

```text
['CenturyArks:silky_common_plugin:00000064']
```

`[]` または `Detected devices: none` の場合は、Windows 側のデバイス状態を確認します。

```powershell
Get-PnpDevice -PresentOnly |
  Where-Object { $_.FriendlyName -match "Silky|Prophesee|Metavision|Event" } |
  Select-Object Status,Class,FriendlyName,InstanceId |
  Format-Table -AutoSize
```

`ProblemCode: 28` や `CM_PROB_FAILED_INSTALL` の場合は、ドライバ未完了です。SilkyEvCam installer を入れ直し、再起動してください。

`[HAL][WARNING] no plugin found` が出る場合は、`MV_HAL_PLUGIN_PATH` または `PATH` が不足しています。

確認:

```powershell
$env:MV_HAL_PLUGIN_PATH
$env:PATH -split ';' | Select-String 'openeb_install|vcpkg'
```

期待される HAL plugin パス:

```text
<workspace>\openeb_install\lib\metavision\hal\plugins
```

## 10. シンプルなRAW撮影サンプル

以下のサンプルは、イベントカメラから RAW を保存します。`Enter` で録画開始/停止、`Esc` または `q` で終了します。

`eventcam_enter_capture.py`:

```python
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from metavision_core.event_io import EventsIterator
from metavision_core.event_io.raw_reader import initiate_device


def main() -> None:
    out_dir = Path("recordings_eventcam")
    out_dir.mkdir(parents=True, exist_ok=True)

    device = initiate_device(path="")
    events_stream = device.get_i_events_stream()
    if events_stream is None:
        raise RuntimeError("This camera does not expose I_EventsStream.")

    iterator = EventsIterator.from_device(
        device=device,
        mode="delta_t",
        delta_t=10_000,
        relative_timestamps=False,
    )

    recording = False
    raw_path = None
    total_events = 0
    first_ts = None
    last_ts = None

    print("Enter: start/stop recording, Esc/q: quit")
    print("This minimal sample has no preview window. Stop with Ctrl+C if needed.")

    try:
        for events in iterator:
            command = input("command [Enter=start/stop, q=quit]: ").strip().lower()
            if command == "q":
                break

            if not recording:
                stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                raw_path = out_dir / f"eventcam_{stamp}.raw"
                events_stream.log_raw_data(str(raw_path.resolve()))
                recording = True
                total_events = 0
                first_ts = None
                last_ts = None
                print(f"Recording: {raw_path}")
                continue

            events_stream.stop_log_raw_data()
            recording = False
            summary_path = raw_path.with_suffix(".json")
            summary = {
                "raw_path": str(raw_path.resolve()),
                "summary_path": str(summary_path.resolve()),
                "total_events": total_events,
                "first_event_ts_us": first_ts,
                "last_event_ts_us": last_ts,
            }
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(f"Stopped: {summary_path}")

            if recording:
                total_events += int(events.size)
                if events.size:
                    if first_ts is None:
                        first_ts = int(events["t"][0])
                    last_ts = int(events["t"][-1])

    finally:
        if recording:
            events_stream.stop_log_raw_data()


if __name__ == "__main__":
    main()
```

実運用では、入力待ちの間にイベントを読めないため、撮影スレッドを分けた実装を推奨します。このリポジトリにある `eventcam_enter_capture.py` のように、UI とイベント読み出しを同時に扱う実装を使うと安定します。

## 11. RAW を後処理する

RAW は通常の動画ではなく、各イベントの `x, y, p, t` が保存されたイベント列です。

|フィールド|意味|
|---|---|
|`x`|画素の横座標|
|`y`|画素の縦座標|
|`p`|極性、ON/OFF イベント|
|`t`|タイムスタンプ、通常はマイクロ秒|

RAW から確認用 MP4 とタイムスタンプ CSV を作る例:

```powershell
python .\eventcam_raw_postprocess.py .\recordings_eventcam\<recording>.json --fps 1000 --video-fps 50
```

意味:

- `--fps 1000`: 1 ms ごとにイベントを画像化
- `--video-fps 50`: MP4 は 50 fps 再生として保存
- 結果: 20倍スロー再生

PNG 連番も保存する場合:

```powershell
python .\eventcam_raw_postprocess.py .\recordings_eventcam\<recording>.json --fps 1000 --video-fps 50 --save-png
```

出力例:

```text
processed_eventcam\<raw名>\
  <raw名>_1000fps_render_50fps_playback.mp4
  <raw名>_1000fps_render_50fps_playback_timestamps.csv
  frames\frame_000000.png
```

ここでの `--fps` はイベントカメラの撮影 fps ではなく、イベント列を何 Hz 相当で可視化するかです。イベントカメラは固定 fps ではなく、輝度変化が起きた画素だけを時刻付きで出します。

## 12. VS Code で使う場合

VS Code では Python extension を使い、インタプリタとして次を選びます。

```text
<workspace>\venv\Scripts\python.exe
```

`.vscode/settings.json` の最小例:

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}\\venv\\Scripts\\python.exe",
  "python.terminal.activateEnvironment": true,
  "terminal.integrated.env.windows": {
    "PYTHONNOUSERSITE": "true"
  }
}
```

OpenEB の DLL / HAL plugin は VS Code terminal を開いた後に、次を実行して設定してください。

```powershell
.\activate_metavision_env.ps1
```

Code Runner を使うと `/usr/bin/env` や `${workspaceFolder}` の扱いで Windows PowerShell と衝突することがあります。Python extension の `Run Python File` または `Debug Python File` を使うのがおすすめです。

## 13. よくあるトラブル

### `ModuleNotFoundError: metavision_hal`

OpenEB の Python binding が venv に入っていません。CMake configure で以下が正しいか確認してください。

```powershell
-DPYTHON3_SITE_PACKAGES="$Workspace\venv\Lib\site-packages"
```

確認:

```powershell
python -c "import metavision_hal; print(metavision_hal.__file__)"
```

### `[HAL][WARNING] no plugin found`

HAL plugin の探索パスが不足しています。

```powershell
$env:MV_HAL_PLUGIN_PATH = "$Workspace\openeb_install\lib\metavision\hal\plugins"
$env:PATH = "$Workspace\openeb_install\bin;$Workspace\vcpkg\installed\x64-windows\bin;$Workspace\openeb_install\lib\metavision\hal\plugins;$env:PATH"
```

### カメラが `[]` / `none`

可能性:

- カメラが接続されていない
- USBケーブル/ポートの問題
- Windows driver が未インストール
- OpenEB とカメラ plugin のバージョン不一致
- サードパーティカメラ用 plugin が入っていない

Windows 側の状態:

```powershell
Get-PnpDevice -PresentOnly |
  Where-Object { $_.FriendlyName -match "Silky|Prophesee|Metavision|Event" } |
  Format-Table -AutoSize
```

### `/usr/bin/env` が PowerShell で認識されない

Code Runner などが Unix 風コマンドで起動しています。Windows PowerShell では次の形で実行します。

```powershell
python .\eventcam_enter_capture.py
```

venv の Python を直指定する場合:

```powershell
& .\venv\Scripts\python.exe .\eventcam_enter_capture.py
```

### `${workspaceFolder}\venv\Scripts\python.exe` が PowerShell でエラー

`${workspaceFolder}` は VS Code の設定ファイル内で展開される変数です。PowerShell に直接貼る文字列ではありません。

PowerShell では次を使います。

```powershell
& .\venv\Scripts\python.exe .\eventcam_enter_capture.py
```

## 14. 最終チェックリスト

環境構築後、次が通ればイベントカメラ環境として一通りOKです。

```powershell
Set-Location $Workspace
.\activate_metavision_env.ps1

python -c "from metavision_core.event_io import EventsIterator; from metavision_core.event_io.raw_reader import initiate_device; import metavision_hal; print('imports OK'); print(metavision_hal.DeviceDiscovery.list())"
python .\eventcam_enter_capture.py --list-devices
python .\eventcam_enter_capture.py
```

撮影後:

```powershell
python .\eventcam_raw_postprocess.py .\recordings_eventcam\<recording>.json --fps 1000 --video-fps 50
```

MP4 は確認用です。厳密な時刻解析には、RAW 内のイベント時刻 `t` と、後処理 CSV の `slice_start_ts_us` / `slice_end_ts_us` を使います。

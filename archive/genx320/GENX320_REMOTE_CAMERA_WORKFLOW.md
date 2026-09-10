# GenX320 Remote Camera Workflow

この構成では、Raspberry Pi 5 を GenX320 カメラサーバ、Windows PC を実験制御・解析マシンとして使う。

## 1. 前提

- Windows から Pi に SSH できる
- Pi 側で GenX320 が `metavision_viewer` から開ける
- Windows 側で `activate_metavision_env.ps1` と `eventcam_raw_postprocess.py` が動く

Pi 接続先の例:

```powershell
$Pi = "eventcamera@192.168.1.23"
```

## 2. VNC 確認

まず VNC を有効化し、必要なら Pi 側デスクトップに `metavision_viewer` を起動する。

```powershell
.\setup_genx320_vnc_remote.ps1 -Pi $Pi -LaunchViewer
```

Windows から VNC Viewer で Pi の IP に接続する。

VNC は映像確認用。録画本番は GUI を使わず、SSH 経由の headless RAW 録画を使う。

## 3. リモート録画と回収

5 秒録画して、RAW/JSON を Windows に回収し、後処理まで実行する。

```powershell
.\run_genx320_remote_capture.ps1 `
  -Pi $Pi `
  -Seconds 5 `
  -Name genx320_test `
  -InstallPiPythonDeps `
  -ExportFilteredEventsNpz
```

出力先:

- RAW/JSON: `rec_eventcam_genx320`
- 後処理結果: `proc_eventcam_genx320`

## 4. LED 同期 ROI 付きで処理

LED 同期マーカーが左上 500x320 にある場合:

```powershell
.\run_genx320_remote_capture.ps1 `
  -Pi $Pi `
  -Seconds 5 `
  -Name genx320_led_sync `
  -SyncLedRoi "0,0,500,320" `
  -MaskLedRoi `
  -ExportFilteredEventsNpz
```

## 5. Pi 側の単体確認

Windows から Pi の状態を見る:

```powershell
ssh $Pi "python3 ~/pi_genx320_camera_server.py status"
ssh $Pi "python3 ~/pi_genx320_camera_server.py list-devices"
```

Pi 側で短時間録画だけ行う:

```powershell
ssh $Pi "python3 ~/pi_genx320_camera_server.py capture --seconds 3 --setup-v4l --basename smoke_test"
```

## 6. 次の統合方針

このリモート録画が安定したら、`acoustools_eventcam_sync.py` には直接組み込まず、まずは Windows 側から `run_genx320_remote_capture.ps1` 相当の処理を呼ぶ薄いアダプタを追加するのが安全。

同期は最初は LED ROI 方式で確認し、必要になったら Trigger In 配線へ進む。

## 7. AcousTools へ近づけるアダプタ

`genx320_remote_eventcam_recorder.py` は、既存の `EventCameraRecorder` に近い形で `start_recording()` / `log_event()` / `stop_recording()` / `close()` を持つ。

使い方の最小例:

```python
from genx320_remote_eventcam_recorder import GenX320RemoteEventCameraRecorder

recorder = GenX320RemoteEventCameraRecorder(
    pi="eventcamera@192.168.100.132",
    save_dir="./rec_eventcam_genx320/test_run",
    postprocess_output_dir="./proc_eventcam_genx320",
    postprocess_fps=1000.0,
    postprocess_video_fps=60.0,
    postprocess_accumulation_us=1000,
)

recorder.start_recording("genx320_api_test")
recorder.log_event("PAT_START")
# PAT send_message(...)
summary = recorder.stop_recording()
recorder.close()
print(summary)
```

このアダプタが安定したら、`acoustools_eventcam_sync.py` のコピーを作り、`EventCameraRecorder(...)` を `GenX320RemoteEventCameraRecorder(...)` に差し替える。

## 8. SSH を録画制御に使わない HTTP サーバ方式

Pi と Windows を Ethernet 直結する場合、Pi 側に HTTP カメラサーバを常駐させると、録画ごとに `ssh` / `scp` を起動しなくてよい。

Pi へサーバを配置して起動:

```powershell
.\setup_genx320_http_server.ps1 -Pi $Pi -Start -SetupOnStart
```

Windows 側から疎通確認:

```powershell
Invoke-RestMethod http://192.168.100.132:8080/status
```

HTTP API smoke test:

```powershell
python test_genx320_http_recorder_api.py `
  --base-url http://192.168.100.132:8080 `
  --seconds 3 `
  --name genx320_http_smoke
```

AcousTools へ近づける場合は、`GenX320RemoteEventCameraRecorder` の代わりに `GenX320HttpEventCameraRecorder` を使う。

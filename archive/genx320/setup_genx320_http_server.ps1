param(
    [Parameter(Mandatory = $true)]
    [string]$Pi,

    [int]$Port = 8080,
    [switch]$Start,
    [switch]$SetupOnStart
)

$ErrorActionPreference = "Stop"
$Workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerScript = Join-Path $Workspace "genx320_http_camera_server.py"

if (-not (Test-Path $ServerScript)) {
    throw "Server script not found: $ServerScript"
}

Write-Host "[GENX320-HTTP] Deploying server to $Pi..."
scp $ServerScript "${Pi}:~/genx320_http_camera_server.py"

if ($Start) {
    $setupArg = if ($SetupOnStart) { "--setup-on-start" } else { "" }
    Write-Host "[GENX320-HTTP] Starting server on Pi port $Port..."
    $remoteCommand = @"
pkill -f '^python3 .*genx320_http_camera_server.py' || true
nohup python3 /home/eventcamera/genx320_http_camera_server.py --host 0.0.0.0 --port $Port $setupArg > /home/eventcamera/genx320_http_camera_server.log 2>&1 < /dev/null &
"@
    ssh $Pi $remoteCommand
    Start-Sleep -Seconds 2
    Write-Host "[GENX320-HTTP] Log tail:"
    ssh $Pi "tail -n 30 ~/genx320_http_camera_server.log || true"
}

Write-Host "[GENX320-HTTP] Test from Windows with:"
Write-Host "  Invoke-RestMethod http://<pi-ip>:$Port/status"

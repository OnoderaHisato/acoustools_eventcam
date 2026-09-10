param(
    [Parameter(Mandatory = $true)]
    [string]$Pi,

    [switch]$LaunchViewer
)

$ErrorActionPreference = "Stop"
$Workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
$PiScript = Join-Path $Workspace "pi_genx320_camera_server.py"

Write-Host "[VNC] Ensuring helper script exists on Pi..."
scp $PiScript "${Pi}:~/pi_genx320_camera_server.py"

Write-Host "[VNC] Trying to enable Raspberry Pi VNC through raspi-config..."
ssh $Pi "sudo raspi-config nonint do_vnc 0 2>/dev/null || true; systemctl --user enable --now wayvnc 2>/dev/null || true; sudo systemctl enable --now vncserver-x11-serviced 2>/dev/null || true; hostname -I"

Write-Host ""
Write-Host "[VNC] Connect from Windows with RealVNC Viewer, TigerVNC Viewer, or Raspberry Pi Connect-compatible tooling."
Write-Host "[VNC] Use the Pi IP printed above. Login with the same Pi desktop user/password."
Write-Host "[VNC] If the custom Prophesee image does not include a VNC server, install/enable one from the Pi desktop once, then rerun this script."

if ($LaunchViewer) {
    Write-Host "[VNC] Launching metavision_viewer on the Pi desktop session..."
    ssh $Pi "DISPLAY=:0 python3 ~/pi_genx320_camera_server.py viewer --setup-v4l >/tmp/genx320_viewer.log 2>&1 &"
    Write-Host "[VNC] Viewer launched. If it does not appear, check /tmp/genx320_viewer.log on the Pi."
}

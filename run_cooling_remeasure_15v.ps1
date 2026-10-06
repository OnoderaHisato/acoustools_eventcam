<#
.SYNOPSIS
Remeasurement after the PAT cooling fans (Experiment/20261006/COOLING_REMEASURE_PLAN_20261006.md).

.DESCRIPTION
-Block 0  Fan on/off check, cold, about 15 minutes, 9 runs, no clock:
          fan OFF: hold_center_30s_fanOFF, kcheck x/y/z -> (switch the fans ON, time recorded)
          fan ON:  hold_center_30s_fanON, kcheck x/y/z  -> (switch the fans OFF, time recorded)
          fan OFF: hold_center_30s_fanOFF
          Before starting: with the fans ON and no sound, check with a strip of tissue that no air
          reaches the space between the boards.
-Block 1  Thermal acceptance, fans ON, cold start, -DurationMin (default 90) by the clock:
          kcheck x/z every 5 minutes from 0 to 60, kcheck x/y/z at 20, 60 and 90,
          hold_center_10s at 0, 30, 60 and 90, wscan_XZ_R28 a/b at 45 and 85.
          (-DurationMin 60 for the 18 V test of section 5 keeps the steps up to 60 minutes, no scans.)
-Block 2  NN training data, about 57 minutes, 67 runs, after -WarmupMin (default 20, the settling time
          measured in block 1; 0 starts at once):
          2a kcheck x/y/z, hold, chirp x 0.05/0.1/(confirm)0.2, chirp y 0.05/0.1/(confirm)0.2, chirp z 0.01/0.02,
             multisine, XL25, (confirm) XL30;  2b the 14 field scans;  2c learning shapes (heart, cardioid a5p4/a10);
          2d test shapes (lissajous, circle, tilted circle, 3-D lissajous, shifted hearts);
          2e kcheck x/y/z, wscan_XZ_R28 a/b, hold, (replace particle) hold, kcheck x, (replace particle) hold, kcheck x.
-Block 12 Block 1 followed directly by block 2 in the same session (no new sound-on); the last kcheck x/y/z
          of block 1 is 2a's first, so 2a starts with the hold.

The current guard (3 %) stays on; its baseline is 40 minutes in blocks 0/1/12 (a rise after that means the
cooling is not enough) and -WarmupMin in block 2. Particle changes and fan switching are recorded in the
session JSON (particle_changes, operator_actions). Output: stereo_acoustools_3d_records_V15c (V18c at 18 V),
separate from the 9/23-9/25 records. Make the PAT-start LED bright (kcheck LED peak 1000 or more).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 0 -DryRunOnly
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 1 -PsuUsb
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 12 -PsuUsb
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 2 -WarmupMin 20 -PsuUsb
powershell -ExecutionPolicy Bypass -File .\run_cooling_remeasure_15v.ps1 -Block 1 -SupplyVoltage 18 -DurationMin 60 -PsuUsb
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("0", "1", "2", "12")]
    [string]$Block,
    [double]$SupplyVoltage = 15.0,
    [string]$OutputDir = "",
    [double]$DurationMin = 90.0,
    [double]$WarmupMin = 20.0,
    [double]$CurrentGuardPercent = 3.0,
    [switch]$NoCurrentGuard,
    [switch]$Unattended,
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    [double]$CaptureTailMarginSec = 5.0,
    [switch]$KeepFailedCaptures,
    [switch]$Regenerate,
    [switch]$PsuUsb,
    [string]$PsuResource = "",
    [string]$PsuHost = "",
    [double]$PsuIntervalSec = 1.0,
    [switch]$DryRunOnly
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "psu_logging.ps1")
Set-Location -LiteralPath $PSScriptRoot

$python = ".\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "venv Python not found: $python" }
$campaign = ".\cooling_20261006"
$plan = Join-Path $campaign "cooling_plan.json"
$export = Join-Path $campaign "export_cooling"
if (-not (Test-Path -LiteralPath $plan)) { throw "Plan not found: $plan" }
if (($SupplyVoltage -le 0.0) -or ($SupplyVoltage -gt 30.0)) { throw "-SupplyVoltage must be in (0, 30]; got $SupplyVoltage" }
if ($DurationMin -lt 60.0) { throw "-DurationMin must be at least 60" }
if ($WarmupMin -lt 0.0) { throw "-WarmupMin must not be negative" }
if ($Block -ne "2" -and $PSBoundParameters.ContainsKey("WarmupMin")) { throw "-WarmupMin applies to -Block 2 only" }
if ($Block -ne "1" -and $Block -ne "12" -and $PSBoundParameters.ContainsKey("DurationMin")) { throw "-DurationMin applies to -Block 1 / 12 only" }
if ($OutputDir -eq "") {
    # Keep the folder name this short: a longer one shortens the run names.
    $voltageTag = if ([math]::Abs($SupplyVoltage - [math]::Round($SupplyVoltage)) -lt 1e-9) { "{0:0}" -f $SupplyVoltage } else { ("{0}" -f $SupplyVoltage).Replace(".", "p") }
    $OutputDir = ".\stereo_acoustools_3d_records_V${voltageTag}c"
}

$manifest = Join-Path $export "export_manifest.json"
$exportIsStale = $true
if (Test-Path -LiteralPath $manifest) {
    $planHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $plan).Hash.ToLowerInvariant()
    try {
        $manifestHash = [string]((Get-Content -Raw -Encoding UTF8 -LiteralPath $manifest | ConvertFrom-Json).source_plan_sha256)
        $exportIsStale = ($manifestHash.ToLowerInvariant() -ne $planHash)
    }
    catch { $exportIsStale = $true }
}
if ($Regenerate -or $exportIsStale) {
    Write-Host "[COOLING] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) { throw "Export generation failed (exit code $LASTEXITCODE)" }
}
else {
    Write-Host "[COOLING] Using existing $export"
}

$runs = New-Object System.Collections.ArrayList
$minutes = New-Object System.Collections.ArrayList
$checkpoints = @()
$particleChange = @()
$actions = @()
function Add-Run([string]$label, $minute = $null) { [void]$runs.Add($label); [void]$minutes.Add($minute) }
$kx = "kcheck_x_S105_6jumps"; $ky = "kcheck_y_S105_6jumps"; $kz = "kcheck_z_S105_6jumps"
$useClock = $false

if ($Block -eq "0") {
    Add-Run "hold_center_30s_fanOFF"; Add-Run $kx; Add-Run $ky; Add-Run $kz
    $actions += "$($runs.Count + 1):switch the cooling fans ON"
    Add-Run "hold_center_30s_fanON"; Add-Run $kx; Add-Run $ky; Add-Run $kz
    $actions += "$($runs.Count + 1):switch the cooling fans OFF"
    Add-Run "hold_center_30s_fanOFF"
    $guardBaseline = 40.0
}
if ($Block -eq "1" -or $Block -eq "12") {
    $useClock = $true
    $scans = $DurationMin -ge 85.0
    $events = @{}
    function Add-Event([double]$m, [string[]]$labels) {
        if (-not $events.ContainsKey($m)) { $events[$m] = New-Object System.Collections.ArrayList }
        foreach ($l in $labels) { [void]$events[$m].Add($l) }
    }
    $holdTimes = @(0.0, 30.0, 60.0, 90.0) | Where-Object { $_ -le $DurationMin }
    foreach ($m in $holdTimes) { Add-Event $m @("hold_center_10s") }
    $xyzTimes = @(20.0, 60.0, $DurationMin) | Sort-Object -Unique
    for ($m = 0.0; $m -le 60.0 + 1e-9; $m += 5.0) {
        if ($xyzTimes -contains $m) { Add-Event $m @($kx, $ky, $kz) } else { Add-Event $m @($kx, $kz) }
    }
    if (-not ($events.ContainsKey($DurationMin) -and $events[$DurationMin].Contains($ky))) { Add-Event $DurationMin @($kx, $ky, $kz) }
    if ($scans) {
        Add-Event 45.0 @("wscan_XZ_R28_a", "wscan_XZ_R28_b")
        Add-Event 85.0 @("wscan_XZ_R28_a", "wscan_XZ_R28_b")
    }
    foreach ($m in ($events.Keys | Sort-Object)) {
        $first = $true
        foreach ($l in $events[$m]) {
            if ($first) { Add-Run $l $m; $first = $false } else { Add-Run $l }
        }
    }
    $guardBaseline = 40.0
}
if ($Block -eq "2" -or $Block -eq "12") {
    if ($Block -eq "2") {
        if ($WarmupMin -gt 0.0) { $useClock = $true; Add-Run $kx $WarmupMin } else { Add-Run $kx }
        Add-Run $ky; Add-Run $kz
        $guardBaseline = if ($WarmupMin -gt 0.0) { $WarmupMin } else { 5.0 }
    }
    # 2a
    Add-Run "hold_center_10s"
    Add-Run "chirp_x_a0.05_40-110Hz"; Add-Run "chirp_x_a0.1_40-110Hz"
    $checkpoints += $runs.Count + 1; Add-Run "chirp_x_a0.2_40-110Hz"
    Add-Run "chirp_y_a0.05_40-110Hz"; Add-Run "chirp_y_a0.1_40-110Hz"
    $checkpoints += $runs.Count + 1; Add-Run "chirp_y_a0.2_40-110Hz"
    Add-Run "chirp_z_a0.01_200-300Hz"; Add-Run "chirp_z_a0.02_200-300Hz"
    Add-Run "multisine_xyz_5-150Hz_8s"
    Add-Run "vzrstep_XL25"
    $checkpoints += $runs.Count + 1; Add-Run "vzrstep_XL30"
    # 2b
    foreach ($surface in @("XZ_R28", "YZ_R28", "XY_R28", "XZ_R24yp8", "XZ_R24ym8", "XZ_R20yp16", "XZ_R20ym16")) {
        Add-Run "wscan_${surface}_a"; Add-Run "wscan_${surface}_b"
    }
    # 2c
    foreach ($d in @("OFF", "OFF", "C_delay", "C_delay", "C_Ws4", "C_Ws4")) { Add-Run "heart_s7_f10_$d" }
    Add-Run "heart_s7_f7_OFF"; Add-Run "heart_s7_f7_C_delay"
    foreach ($d in @("OFF", "C_delay", "C_Ws4")) { Add-Run "cardioid_a5p4_f10_$d" }
    foreach ($d in @("OFF", "C_delay", "C_nl")) { Add-Run "cardioid_a10_f10_$d" }
    # 2d
    foreach ($shape in @("lissajous_s15", "circle_s20", "circle_tilt30", "lissajous3d_s13")) {
        Add-Run "${shape}_f10_OFF"; Add-Run "${shape}_f10_C_delay"
    }
    foreach ($o in @("xp15", "xm15", "zp15", "zm15")) { Add-Run "heart_s7_${o}_f10_C_Ws4" }
    foreach ($o in @("yp10", "ym10")) { Add-Run "heart_s7_${o}_f10_C_W3" }
    # 2e
    Add-Run $kx; Add-Run $ky; Add-Run $kz
    Add-Run "wscan_XZ_R28_a"; Add-Run "wscan_XZ_R28_b"
    Add-Run "hold_center_10s"
    $particleChange += $runs.Count + 1; Add-Run "hold_center_10s"; Add-Run $kx
    $particleChange += $runs.Count + 1; Add-Run "hold_center_10s"; Add-Run $kx
}

$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$recordArgs = @(
    "--hf-export-dir", $export,
    "--output-dir", $OutputDir,
    "--capture-tail-margin-sec", $CaptureTailMarginSec.ToString($invariant),
    "--supply-voltage-v", $SupplyVoltage.ToString($invariant),
    "--unattended-after-first-checkpoint"
)
foreach ($label in $runs) { $recordArgs += @("--hf-run", "$label=1") }
if ($useClock) {
    $offsets = @()
    foreach ($minute in $minutes) {
        if ($null -eq $minute) { $offsets += "" } else { $offsets += (60.0 * [double]$minute).ToString($invariant) }
    }
    $recordArgs += @("--schedule-offsets-sec", ($offsets -join ","))
}
if ($checkpoints.Count -gt 0) { $recordArgs += @("--checkpoint-run-numbers", ($checkpoints -join ",")) }
if ($particleChange.Count -gt 0) { $recordArgs += @("--particle-change-run-numbers", ($particleChange -join ",")) }
if ($actions.Count -gt 0) { $recordArgs += @("--operator-actions", ($actions -join ";")) }
if ($KeepFailedCaptures) { $recordArgs += "--keep-failed-captures" }
if ($Unattended) { $recordArgs += @("--automatic-capture-retries", "$AutomaticCaptureRetries") }
else { $recordArgs += "--prompt-on-capture-failure" }

Write-Host ("[COOLING] Block {0}, {1} V, {2} runs, output {3}." -f $Block, $SupplyVoltage, $runs.Count, $OutputDir)
$actionAt = @{}
foreach ($a in $actions) { $n, $text = $a.Split(":", 2); $actionAt[[int]$n] = $text }
$index = 0
foreach ($label in $runs) {
    $index += 1
    $at = if ($null -eq $minutes[$index - 1]) { "     " } else { "{0,3} m" -f $minutes[$index - 1] }
    $mark = ""
    if ($actionAt.ContainsKey($index)) { $mark = "  <- " + $actionAt[$index] + " (time recorded)" }
    elseif ($particleChange -contains $index) { $mark = "  <- replace the particle (time recorded)" }
    elseif ($checkpoints -contains $index) { $mark = "  <- confirm" }
    Write-Host ("[COOLING]  {0,3}. {1}  {2}{3}" -f $index, $at, $label, $mark)
}

Write-Host "[COOLING] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) { throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened." }
if ($DryRunOnly) {
    Write-Host "[COOLING] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[COOLING] Check before starting:"
Write-Host "  - Supply at $SupplyVoltage V. Fans: block 0 starts with the fans OFF; blocks 1/2/12 run with the fans ON."
if ($Block -eq "0") { Write-Host "  - Before sound: fans ON, check with a strip of tissue that no air reaches the space between the boards; then fans OFF." }
Write-Host "  - PAT-start LED bright: kcheck LED peak count 1000 or more."
Write-Host "  - Note room / board temperatures (thermal_log_template.csv with fan_state, fan_rpm_or_V)."
Write-Host "  - Stop with Ctrl+C if the particle is gone; the session then stops and PAT is turned off."
$psuTarget = Get-PsuTargetArgs -Usb:$PsuUsb -Resource $PsuResource -HostName $PsuHost
$psuLogger = Start-PsuLogger -Python $python -OutputDir $OutputDir -TargetArgs $psuTarget -IntervalSec $PsuIntervalSec
if ($null -ne $psuLogger) { $recordArgs += @("--psu-log", $psuLogger.Log) }
$recordArgs += Get-CurrentGuardArgs -Logger $psuLogger -Percent $CurrentGuardPercent -BaselineMin $guardBaseline -Disabled:$NoCurrentGuard
try {
    & $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs `
        --acknowledge-ff-validation-protocol --acknowledge-step-response-risk
    $code = $LASTEXITCODE
}
finally {
    Stop-PsuLogger $psuLogger
    Write-PsuReport -Python $python -OutputDir $OutputDir -Logger $psuLogger
}
if ($code -eq 0) { Write-Host "[COOLING] Block $Block finished. Output: $OutputDir" }
elseif ($code -eq 3) { Write-Host "[COOLING] The current guard stopped the block before the next run: the cooling is not enough (see current_guard in the session JSON). Rest 45 minutes or more; restart with kcheck x/y/z and hold_center_10s first." }
else { Write-Host "[COOLING] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir." }
exit $code

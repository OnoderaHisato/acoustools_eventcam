<#
.SYNOPSIS
Scale-up session at the 15 V operating point (SCALE_UP_PLAN_20260924.md section 3, about 70 min):
26 / 44 / 60 mm cardioids at 10 Hz, the 28 mm radius w scan, and the 2.5 / 3.0 mm horizontal steps.

.DESCRIPTION
Order: kcheck x/y/z -> XL25 -> (confirm the particle) XL30 -> wscan R28 a/b -> kcheck x/z
       -> a10 OFF, C, C_nl, C, OFF -> a17 the same five -> kcheck x/z
       -> (confirm) a23 the same five -> kcheck x/y/z
Warm-up: by default the script starts from the cold state like the 14 mm session. Right after
PAT opens (sound on) a stereo preview asks you to confirm the particle; then it waits
-WarmupMin minutes (default 40, when the stiffness levelled off at 15 V) with kcheck x/z every
5 minutes, and the plan's sequence starts at that minute. Right after the 14 mm session, when
the boards are already warm, pass -WarmupMin 0 to start at once (the first run then has the
preview instead). The 3.0 mm step and the first 60 mm cardioid also stop for a preview and
Enter. A failed capture asks "re-record? (Y/n)". Particle loss is NOT detected automatically.

These commands go past the limits used until 2026-09-24 (raised with the user's approval):
offset 35 mm, speed 3500 mm/s, acceleration 350,000 mm/s^2, |u-r| 4 mm, staircase jump 3.2 mm.
The 60 mm cardioid reaches 30.7 mm from the centre and 2890 mm/s; the horizontal steps are
past the vertical lambda/4 estimate, which the analysis side says does not apply sideways.
Losing the particle is a real possibility - watch the preview and stop with Ctrl+C if it goes.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_scaleup_20260924.ps1 -DryRunOnly

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_scaleup_20260924.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_scaleup_20260924.ps1 -Sizes "a10,a17"

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_scaleup_20260924.ps1 -WarmupMin 0
#>
[CmdletBinding()]
param(
    [double]$SupplyVoltage = 15.0,
    [string]$OutputDir = ".\stereo_acoustools_3d_records_V15",
    # Cardioid sizes to record, in order (a10 = 26 mm, a17 = 44 mm, a23 = 60 mm).
    [string]$Sizes = "a10,a17,a23",
    # Minutes after sound on before the plan's sequence starts (0 = start at once, for a
    # session that follows the 14 mm session while the boards are still warm).
    [double]$WarmupMin = 40.0,
    # Do not record kcheck x/z every 5 minutes during the warm-up; just wait.
    [switch]$SkipWarmupChecks,
    # Skip the 2.5 / 3.0 mm horizontal steps.
    [switch]$SkipLargeSteps,
    # Skip the 28 mm radius w scan.
    [switch]$SkipScan,
    # Also record A_delay and OT_ident for each size (plan item 8, "if there is time").
    [switch]$IncludeExtraDesigns,
    [switch]$Unattended,
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    [double]$CaptureTailMarginSec = 5.0,
    [switch]$KeepFailedCaptures,
    [switch]$Regenerate,
    # Log the PAT supply (Kikusui PWR801L) during the session: -PsuUsb finds it on USB (needs a
    # VISA library such as KI-VISA), -PsuResource names a VISA resource, -PsuHost uses the LAN.
    # The logger is read-only and is started before PAT opens. See PSU_LOGGING_JP.md.
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
$campaign = ".\scaleup_20260924"
$plan = Join-Path $campaign "scaleup_plan.json"
$export = Join-Path $campaign "export_scaleup"
if (-not (Test-Path -LiteralPath $plan)) { throw "Plan not found: $plan" }
if (($SupplyVoltage -le 0.0) -or ($SupplyVoltage -gt 30.0)) {
    throw "-SupplyVoltage must be in (0, 30]; got $SupplyVoltage"
}
$selectedSizes = @($Sizes.Split(",") | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ -ne "" })
foreach ($size in $selectedSizes) {
    if ($size -notin @("a10", "a17", "a23")) { throw "Unknown size '$size'. Use a10, a17, a23." }
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
    Write-Host "[SCALEUP] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) { throw "Export generation failed (exit code $LASTEXITCODE)" }
}
else {
    Write-Host "[SCALEUP] Using existing $export"
}

if ($WarmupMin -lt 0.0) { throw "-WarmupMin must not be negative" }
$runs = New-Object System.Collections.ArrayList
$minutes = New-Object System.Collections.ArrayList
function Add-Run([string]$label, $minute = $null) { [void]$runs.Add($label); [void]$minutes.Add($minute) }
$checkpoints = @()

if ($WarmupMin -gt 0.0 -and -not $SkipWarmupChecks) {
    for ($minute = 5.0; $minute -lt $WarmupMin - 1e-9; $minute += 5.0) {
        Add-Run "kcheck_x_S105_6jumps" $minute
        Add-Run "kcheck_z_S105_6jumps"
    }
}
Add-Run "kcheck_x_S105_6jumps" $(if ($WarmupMin -gt 0.0) { $WarmupMin } else { $null })
Add-Run "kcheck_y_S105_6jumps"; Add-Run "kcheck_z_S105_6jumps"
if (-not $SkipLargeSteps) {
    Add-Run "vzrstep_XL25"
    $checkpoints += $runs.Count + 1      # confirm the particle survived 2.5 mm before 3.0 mm
    Add-Run "vzrstep_XL30"
}
if (-not $SkipScan) { Add-Run "wscan_XZ_R28_a"; Add-Run "wscan_XZ_R28_b" }
Add-Run "kcheck_x_S105_6jumps"; Add-Run "kcheck_z_S105_6jumps"
$sizeIndex = 0
foreach ($size in $selectedSizes) {
    $sizeIndex += 1
    if ($size -eq "a23") { $checkpoints += $runs.Count + 1 }   # 60 mm: confirm before starting
    $designs = if ($IncludeExtraDesigns) {
        @("OFF", "A_delay", "C_delay", "C_nl", "C_delay", "OT_ident", "OFF")
    }
    else {
        @("OFF", "C_delay", "C_nl", "C_delay", "OFF")
    }
    foreach ($design in $designs) { Add-Run "cardioid_${size}_f10_${design}" }
    if ($sizeIndex -lt $selectedSizes.Count) {
        Add-Run "kcheck_x_S105_6jumps"; Add-Run "kcheck_z_S105_6jumps"
    }
}
Add-Run "kcheck_x_S105_6jumps"; Add-Run "kcheck_y_S105_6jumps"; Add-Run "kcheck_z_S105_6jumps"

$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$recordArgs = @(
    "--hf-export-dir", $export,
    "--output-dir", $OutputDir,
    "--capture-tail-margin-sec", $CaptureTailMarginSec.ToString($invariant),
    "--supply-voltage-v", $SupplyVoltage.ToString($invariant),
    "--unattended-after-first-checkpoint"
)
foreach ($label in $runs) { $recordArgs += @("--hf-run", "$label=1") }
if ($WarmupMin -gt 0.0) {
    # Timed start: the particle is confirmed right after sound on, then the runs follow the clock.
    $offsets = @()
    foreach ($minute in $minutes) {
        if ($null -eq $minute) { $offsets += "" } else { $offsets += (60.0 * [double]$minute).ToString($invariant) }
    }
    $recordArgs += @("--schedule-offsets-sec", ($offsets -join ","))
}
if ($checkpoints.Count -gt 0) { $recordArgs += @("--checkpoint-run-numbers", ($checkpoints -join ",")) }
if ($KeepFailedCaptures) { $recordArgs += "--keep-failed-captures" }
if ($Unattended) { $recordArgs += @("--automatic-capture-retries", "$AutomaticCaptureRetries") }
else { $recordArgs += "--prompt-on-capture-failure" }

Write-Host ("[SCALEUP] {0} V, {1} runs. Extra checkpoints before run(s): {2}" -f `
    $SupplyVoltage, $runs.Count, ($(if ($checkpoints.Count) { $checkpoints -join ", " } else { "none" })))
$index = 0
foreach ($label in $runs) {
    $index += 1
    $mark = if ($checkpoints -contains $index) { "<- confirm" } else { "" }
    $at = if ($null -eq $minutes[$index - 1]) { "     " } else { "{0,3} m" -f $minutes[$index - 1] }
    Write-Host ("[SCALEUP]  {0,3}. {1}  {2,-26} {3}" -f $index, $at, $label, $mark)
}

Write-Host "[SCALEUP] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) { throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened." }
if ($DryRunOnly) {
    Write-Host "[SCALEUP] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[SCALEUP] Check before starting:"
if ($WarmupMin -gt 0.0) {
    Write-Host "  - Supply at $SupplyVoltage V and the boards cold. Confirm the particle right after PAT opens;"
    Write-Host "    the sequence starts $WarmupMin min after sound on."
}
else {
    Write-Host "  - Supply at $SupplyVoltage V and the boards already settled (-WarmupMin 0)."
}
Write-Host "  - These commands reach 30.7 mm from the centre and 2890 mm/s; the particle may be lost."
Write-Host "  - Stop with Ctrl+C at a preview if the particle is gone; the session then stops and PAT is turned off."
$psuTarget = Get-PsuTargetArgs -Usb:$PsuUsb -Resource $PsuResource -HostName $PsuHost
$psuLogger = Start-PsuLogger -Python $python -OutputDir $OutputDir -TargetArgs $psuTarget -IntervalSec $PsuIntervalSec
if ($null -ne $psuLogger) { $recordArgs += @("--psu-log", $psuLogger.Log) }
try {
    & $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs `
        --acknowledge-ff-validation-protocol --acknowledge-step-response-risk
    $code = $LASTEXITCODE
}
finally {
    Stop-PsuLogger $psuLogger
    Write-PsuReport -Python $python -OutputDir $OutputDir -Logger $psuLogger
}
if ($code -eq 0) { Write-Host "[SCALEUP] The session finished. Output: $OutputDir" }
else { Write-Host "[SCALEUP] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir." }
exit $code

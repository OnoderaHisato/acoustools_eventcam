<#
.SYNOPSIS
Field-mapping session at the 15 V operating point (Experiment/20260924/FIELD_MAPPING_SESSION.md):
w scans on 7 surfaces x a/b, recorded twice, to fit the equilibrium-shift field w(x, y, z).

.DESCRIPTION
Order (clock = minutes after sound on, i.e. after PAT output starts):
   5..35 min  kcheck x/z every 5 min (warm-up; -SkipWarmupChecks just waits)
      40 min  kcheck x/y/z -> 14 scans (round 1) -> kcheck x/z
   rest       kcheck x/z every 5 min, the particle stays levitated (operating state)
  -Round2StartMin (default 100)  14 scans (round 2) -> kcheck x/y/z
The 14 scans are, surface by surface, a (counter-clockwise) then b (clockwise):
  XZ_R28, YZ_R28, XY_R28, XZ_R24yp8, XZ_R24ym8, XZ_R20yp16, XZ_R20ym16.
Round 1 should end at about 63 min, so the default round-2 start leaves more than 30 minutes of
rest. If round 1 runs late the rest is shorter: check the times in the session JSON.

Only the particle check right after PAT opens stops for a preview and Enter; the rest runs by
the clock. A failed capture asks "re-record? (Y/n)". Particle loss is NOT detected automatically.
Write down the room temperature at the start and at the end. Log the supply with -PsuUsb.

Thermal note: on 2026-09-24 one PAT board tripped its fuse 83 minutes after sound on at 15 V
(heavy 14 mm trajectories). This session keeps the sound on for about 2 hours, although the scans
themselves are slow (at most 264 mm/s, 2.5k mm/s^2).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_fieldscan_15v.ps1 -DryRunOnly

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_fieldscan_15v.ps1 -PsuUsb

.EXAMPLE
# One round per session (about an hour of sound each), 45 min or more without sound in between:
powershell -ExecutionPolicy Bypass -File .\run_fieldscan_15v.ps1 -Round 1 -PsuUsb
powershell -ExecutionPolicy Bypass -File .\run_fieldscan_15v.ps1 -Round 2 -PsuUsb

Current guard: with a supply log (-PsuUsb) the session stops before the next run when a PAT board
drops out or the current is -CurrentGuardPercent (default 3, chosen 2026-09-24) above its value at the end of the
warm-up. -NoCurrentGuard turns it off.
#>
[CmdletBinding()]
param(
    [double]$SupplyVoltage = 15.0,
    [string]$OutputDir = ".\stereo_acoustools_3d_records_V15",
    # Minutes after sound on before round 1 starts (with its kcheck x/y/z).
    [double]$WarmupMin = 40.0,
    # Minutes after sound on at which round 2 starts. Keep it at least 30 min after round 1 ends.
    [double]$Round2StartMin = 100.0,
    # Do not record kcheck x/z every 5 minutes during the warm-up and the rest; just wait.
    [switch]$SkipWarmupChecks,
    # Record one round only, from the cold state with its own warm-up (about an hour of sound):
    # warm-up -> kcheck x/y/z -> 14 scans -> kcheck x/z. Leave 45 min or more without sound
    # between -Round 1 and -Round 2. Both (default) records the two rounds in one session.
    [ValidateSet("Both", "1", "2")]
    [string]$Round = "Both",
    # Re-record only these plan runs (comma separated): warm-up -> kcheck x/y/z -> the named runs.
    # -WarmupMin 0 starts at once without the warm-up (the boards are then not at the operating
    # temperature). Cannot be combined with -Round or -Round2StartMin.
    [string]$RetryRuns = "",
    # Stop before the next run when a PAT board drops out or the supply current has risen this
    # many percent above its value at the end of the warm-up (needs -PsuUsb/-PsuResource/-PsuHost).
    [double]$CurrentGuardPercent = 3.0,
    [switch]$NoCurrentGuard,
    [switch]$Unattended,
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    [double]$CaptureTailMarginSec = 5.0,
    [switch]$KeepFailedCaptures,
    [switch]$Regenerate,
    # Log the PAT supply (Kikusui PWR801L): -PsuUsb finds it on USB (needs a VISA library such as
    # KI-VISA), -PsuResource names a VISA resource, -PsuHost uses the LAN. See PSU_LOGGING_JP.md.
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
$campaign = ".\fieldscan_15V"
$plan = Join-Path $campaign "fieldscan_15V_plan.json"
$export = Join-Path $campaign "export_fieldscan"
if (-not (Test-Path -LiteralPath $plan)) { throw "Plan not found: $plan" }
if (($SupplyVoltage -le 0.0) -or ($SupplyVoltage -gt 30.0)) {
    throw "-SupplyVoltage must be in (0, 30]; got $SupplyVoltage"
}
$retryMode = ($RetryRuns.Trim() -ne "")
if ($retryMode) {
    if ($PSBoundParameters.ContainsKey("Round") -or $PSBoundParameters.ContainsKey("Round2StartMin")) {
        throw "-RetryRuns cannot be combined with -Round or -Round2StartMin."
    }
    if ($WarmupMin -lt 0.0) { throw "-WarmupMin must not be negative" }
}
elseif ($WarmupMin -le 0.0) { throw "-WarmupMin must be positive (0 is allowed only with -RetryRuns)" }
if ($Round -ne "Both" -and $PSBoundParameters.ContainsKey("Round2StartMin")) {
    throw "-Round2StartMin applies only when both rounds are recorded in one session (-Round Both)."
}
if (-not $retryMode -and $Round -eq "Both" -and $Round2StartMin -lt $WarmupMin + 50.0) {
    throw "-Round2StartMin ($Round2StartMin) must be at least 50 min after the warm-up (round 1 takes about 23 min, then 30 min of rest)"
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
    Write-Host "[FIELDSCAN] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) { throw "Export generation failed (exit code $LASTEXITCODE)" }
}
else {
    Write-Host "[FIELDSCAN] Using existing $export"
}

$surfaces = @("XZ_R28", "YZ_R28", "XY_R28", "XZ_R24yp8", "XZ_R24ym8", "XZ_R20yp16", "XZ_R20ym16")
$runs = New-Object System.Collections.ArrayList
$minutes = New-Object System.Collections.ArrayList
function Add-Run([string]$label, $minute = $null) { [void]$runs.Add($label); [void]$minutes.Add($minute) }
function Add-Scans($firstMinute) {
    $first = $true
    foreach ($surface in $surfaces) {
        foreach ($side in @("a", "b")) {
            if ($first) { Add-Run "wscan_${surface}_${side}" $firstMinute; $first = $false }
            else { Add-Run "wscan_${surface}_${side}" }
        }
    }
}

if ($WarmupMin -gt 0.0 -and -not $SkipWarmupChecks) {
    for ($minute = 5.0; $minute -lt $WarmupMin - 1e-9; $minute += 5.0) {
        Add-Run "kcheck_x_S105_6jumps" $minute
        Add-Run "kcheck_z_S105_6jumps"
    }
}
Add-Run "kcheck_x_S105_6jumps" $(if ($WarmupMin -gt 0.0) { $WarmupMin } else { $null })
Add-Run "kcheck_y_S105_6jumps"; Add-Run "kcheck_z_S105_6jumps"
if ($retryMode) {
    # Re-recording session: only the named runs after the warm-up and its kcheck x/y/z.
    $known = @((Get-Content -Raw -Encoding UTF8 -LiteralPath $plan | ConvertFrom-Json).experiments | ForEach-Object { $_.name })
    foreach ($label in @($RetryRuns.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })) {
        if ($known -notcontains $label) { throw "Unknown run '$label'. Use the plan names, e.g. wscan_XZ_R20ym16_b." }
        Add-Run $label
    }
}
else {
    Add-Scans $null
    Add-Run "kcheck_x_S105_6jumps"; Add-Run "kcheck_z_S105_6jumps"
}
if (-not $retryMode -and $Round -eq "Both") {
    if (-not $SkipWarmupChecks) {
        # kcheck x/z every 5 min during the rest, starting 25 min after the warm-up ends
        for ($minute = $WarmupMin + 25.0; $minute -lt $Round2StartMin - 1e-9; $minute += 5.0) {
            Add-Run "kcheck_x_S105_6jumps" $minute
            Add-Run "kcheck_z_S105_6jumps"
        }
    }
    Add-Scans $Round2StartMin
    Add-Run "kcheck_x_S105_6jumps"; Add-Run "kcheck_y_S105_6jumps"; Add-Run "kcheck_z_S105_6jumps"
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
$offsets = @()
foreach ($minute in $minutes) {
    if ($null -eq $minute) { $offsets += "" } else { $offsets += (60.0 * [double]$minute).ToString($invariant) }
}
if ($WarmupMin -gt 0.0) { $recordArgs += @("--schedule-offsets-sec", ($offsets -join ",")) }
if ($KeepFailedCaptures) { $recordArgs += "--keep-failed-captures" }
if ($Unattended) { $recordArgs += @("--automatic-capture-retries", "$AutomaticCaptureRetries") }
else { $recordArgs += "--prompt-on-capture-failure" }

if ($retryMode) {
    Write-Host ("[FIELDSCAN] {0} V, re-recording {1} runs (warm-up {2} min)." -f $SupplyVoltage, $runs.Count, $WarmupMin)
}
elseif ($Round -eq "Both") {
    Write-Host ("[FIELDSCAN] {0} V, {1} runs (28 scans). Round 1 at {2} min, round 2 at {3} min after sound on." -f `
        $SupplyVoltage, $runs.Count, $WarmupMin, $Round2StartMin)
}
else {
    Write-Host ("[FIELDSCAN] {0} V, round {1} only, {2} runs (14 scans) starting {3} min after sound on." -f `
        $SupplyVoltage, $Round, $runs.Count, $WarmupMin)
}
$index = 0
foreach ($label in $runs) {
    $index += 1
    $at = if ($null -eq $minutes[$index - 1]) { "     " } else { "{0,3} m" -f $minutes[$index - 1] }
    Write-Host ("[FIELDSCAN]  {0,3}. {1}  {2}" -f $index, $at, $label)
}

Write-Host "[FIELDSCAN] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) { throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened." }
if ($DryRunOnly) {
    Write-Host "[FIELDSCAN] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[FIELDSCAN] Check before starting:"
Write-Host "  - Supply at $SupplyVoltage V and the boards cold (45 min or more without sound)."
Write-Host "  - Write down the room temperature now and at the end."
Write-Host "  - Confirm the particle at the preview right after PAT opens; after that the runs follow the clock."
Write-Host "  - Stop with Ctrl+C if the particle is gone; the session then stops and PAT is turned off."
$psuTarget = Get-PsuTargetArgs -Usb:$PsuUsb -Resource $PsuResource -HostName $PsuHost
$psuLogger = Start-PsuLogger -Python $python -OutputDir $OutputDir -TargetArgs $psuTarget -IntervalSec $PsuIntervalSec
if ($null -ne $psuLogger) { $recordArgs += @("--psu-log", $psuLogger.Log) }
$recordArgs += Get-CurrentGuardArgs -Logger $psuLogger -Percent $CurrentGuardPercent -BaselineMin $(if ($WarmupMin -gt 0.0) { $WarmupMin } else { 5.0 }) -Disabled:$NoCurrentGuard
try {
    & $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs `
        --acknowledge-ff-validation-protocol --acknowledge-step-response-risk
    $code = $LASTEXITCODE
}
finally {
    Stop-PsuLogger $psuLogger
    Write-PsuReport -Python $python -OutputDir $OutputDir -Logger $psuLogger
}
if ($code -eq 0) { Write-Host "[FIELDSCAN] The session finished. Output: $OutputDir" }
elseif ($code -eq 3) { Write-Host "[FIELDSCAN] The current guard stopped the session before the next run (see current_guard in the session JSON). Let the boards cool before continuing." }
else { Write-Host "[FIELDSCAN] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir." }
exit $code

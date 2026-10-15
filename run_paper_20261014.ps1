<#
.SYNOPSIS
Paper sessions S1-S4 (Experiment/20261014/PAPER_SESSION_PLAN_20261014.md, runlist/S*_runlist.csv): the training
data for the full paper, recorded from scratch. No compensation runs.

.DESCRIPTION
The run order is read from paper_20261014/runlist/<session>_runlist.csv (one line per run, as the analysis side
wrote it). -Session takes one session or two to record in one PAT connection on the same day, for example
"S1,S2" (day 1) or "S3,S4" (day 2). When a second session follows directly, its leading kcheck/hold block is
dropped (the previous session's closing kcheck/hold stands in for it; -KeepHead keeps it). A "change" line in
the runlist becomes a particle-change stop before the next run (the script asks for the new particle and Enter;
the time goes to particle_changes in the session JSON).

Fans ON, 15 V, current guard 3 %, temperatures asked every 10 minutes, environment (Tempo Disc) and PAT board
temperatures (DS18B20) logged when the sensors are present. By the clock only the first run waits for -WarmupMin
(default 40) minutes after sound-on; everything after it runs at once. Extra confirmations (stereo preview and
Enter) are inserted before cardioid_a23_f10_OFF, the first random_rms10_*, and the first rotline_* run.

If the particle falls, stop with Ctrl+C, replace it, and resume with -WarmupMin 0 -StartAt <run> (the run
numbers are those printed by this script for the same -Session; particle changes after that point are kept).

At the end (complete, current guard, failure, Ctrl+C) the PAT keeps holding the particle at the centre until
Enter is pressed, so the particle can be collected (-NoHoldAtEnd turns that off). A notification is sent when the
session ends or stops if notify_config.json exists (see PAPER_SESSION_JP.md).

Output: stereo_acoustools_3d_records_V15c (run folders carry the date).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_paper_20261014.ps1 -Session S1 -DryRunOnly
powershell -ExecutionPolicy Bypass -File .\run_paper_20261014.ps1 -Session S1,S2 -PsuUsb
powershell -ExecutionPolicy Bypass -File .\run_paper_20261014.ps1 -Session S3,S4 -PsuUsb
powershell -ExecutionPolicy Bypass -File .\run_paper_20261014.ps1 -Session S1,S2 -WarmupMin 0 -StartAt 57 -PsuUsb
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Session,
    [double]$SupplyVoltage = 15.0,
    [string]$OutputDir = "",
    [double]$WarmupMin = 40.0,
    [double]$CurrentGuardPercent = 3.0,
    [switch]$NoCurrentGuard,
    [switch]$Unattended,
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    [double]$CaptureTailMarginSec = 5.0,
    [switch]$KeepFailedCaptures,
    [switch]$KeepHead,
    [int]$StartAt = 1,
    [switch]$NoThermalPrompts,
    [double]$ThermalPromptTimeoutSec = 180.0,
    [string]$PatBoardGapMm = "236.5-237",
    [switch]$NoHoldAtEnd,
    [switch]$NoParticlePrompts,
    [ValidateSet("pause", "warn", "off")]
    [string]$ParticleCheck = "pause",
    [string]$Calipers = "",
    [string]$MeasuredBy = "",
    [string]$ParticleSource = "",
    [switch]$Regenerate,
    [switch]$PsuUsb,
    [string]$PsuResource = "",
    [string]$PsuHost = "",
    [double]$PsuIntervalSec = 1.0,
    [string]$EnvDevice = "F9EE9FFC",
    [switch]$NoEnvSensor,
    [string]$PatTempPort = "auto",
    [string]$PatTempSensorMap = "",
    [switch]$NoPatTempSensor,
    [switch]$DryRunOnly
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "psu_logging.ps1")
. (Join-Path $PSScriptRoot "env_logging.ps1")
. (Join-Path $PSScriptRoot "pat_temp_logging.ps1")
Set-Location -LiteralPath $PSScriptRoot

$python = ".\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "venv Python not found: $python" }
$campaign = ".\paper_20261014"
$plan = Join-Path $campaign "paper_plan.json"
$export = Join-Path $campaign "export_paper"
if (-not (Test-Path -LiteralPath $plan)) { throw "Plan not found: $plan" }
if (($SupplyVoltage -le 0.0) -or ($SupplyVoltage -gt 30.0)) { throw "-SupplyVoltage must be in (0, 30]; got $SupplyVoltage" }
if ($WarmupMin -lt 0.0) { throw "-WarmupMin must not be negative" }
# -Session may come as "S1,S2" (one string) or S1,S2 (array)
$sessions = @()
foreach ($s in $Session) { foreach ($part in ($s -split ",")) { if ($part.Trim() -ne "") { $sessions += $part.Trim().ToUpperInvariant() } } }
if ($sessions.Count -lt 1 -or $sessions.Count -gt 2) { throw "-Session takes one or two of S1, S2, S3, S4 (for example S1,S2)" }
foreach ($s in $sessions) { if ($s -notin @("S1", "S2", "S3", "S4")) { throw "Unknown session '$s' (S1, S2, S3 or S4)" } }
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
    Write-Host "[PAPER] Generating $export from $plan (296 commands; this takes a few minutes)"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) { throw "Export generation failed (exit code $LASTEXITCODE)" }
}
else {
    Write-Host "[PAPER] Using existing $export"
}

# ---- run order from the runlist CSVs -------------------------------------------------------------------
$runs = New-Object System.Collections.ArrayList     # run labels
$minutes = New-Object System.Collections.ArrayList  # clock minute of the first run only
$recordSec = New-Object System.Collections.ArrayList
$blocks = New-Object System.Collections.ArrayList
$particleOf = New-Object System.Collections.ArrayList  # particle label of each run (runlist column)
$changeBefore = @{}                                  # run number -> particle label
$pendingChange = $null
$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$sessionIndex = 0
foreach ($s in $sessions) {
    $csv = Join-Path $campaign ("runlist\{0}_runlist.csv" -f $s)
    if (-not (Test-Path -LiteralPath $csv)) { throw "Runlist not found: $csv" }
    $rows = @(Import-Csv -LiteralPath $csv -Encoding UTF8)
    if ($sessionIndex -gt 0 -and -not $KeepHead -and $rows[0].kind -ne "change") {
        # The previous session's closing kcheck/hold stands in for this session's opening block.
        $dropped = 0
        while ($rows.Count -gt 0 -and $rows[0].kind -eq "kcheck") { $rows = @($rows | Select-Object -Skip 1); $dropped += 1 }
        Write-Host ("[PAPER] {0}: dropped its opening {1} kcheck/hold run(s); the end of {2} stands in (-KeepHead keeps them)." -f $s, $dropped, $sessions[$sessionIndex - 1])
    }
    foreach ($row in $rows) {
        if ($row.kind -eq "change") { $pendingChange = [string]$row.particle; continue }
        [void]$runs.Add($row.run)
        [void]$minutes.Add($null)
        [void]$recordSec.Add([double]::Parse($row.record_s, $invariant))
        [void]$blocks.Add(("{0}/{1}/p{2}" -f $row.session, $row.block, $row.particle))
        [void]$particleOf.Add([string]$row.particle)
        if ($null -ne $pendingChange) { $changeBefore[$runs.Count] = $pendingChange; $pendingChange = $null }
    }
    $sessionIndex += 1
}
if ($null -ne $pendingChange) { throw "The runlist ends with a particle change" }
if ($WarmupMin -gt 0.0) { $useClock = $true; $minutes[0] = $WarmupMin } else { $useClock = $false }

# Extra confirmations: before the largest cardioid, the first rms-10 random and the first rotating line.
$checkpoints = @()
foreach ($pattern in @("cardioid_a23_f10_OFF", "random_rms10_*", "rotline_*")) {
    for ($i = 0; $i -lt $runs.Count; $i++) {
        if ($runs[$i] -like $pattern) { if (($i + 1) -gt 1) { $checkpoints += ($i + 1) }; break }
    }
}
$guardBaseline = if ($WarmupMin -gt 0.0) { $WarmupMin } else { 5.0 }

if ($StartAt -lt 1 -or $StartAt -gt $runs.Count) { throw "-StartAt must be between 1 and $($runs.Count)" }
if ($StartAt -gt 1) {
    # Resume from run $StartAt of the same list at once (no clock); the runs are renumbered from 1.
    $useClock = $false
    $shift = $StartAt - 1
    $runs = [System.Collections.ArrayList]@($runs[$shift..($runs.Count - 1)])
    $minutes = [System.Collections.ArrayList]@($minutes[$shift..($minutes.Count - 1)])
    $recordSec = [System.Collections.ArrayList]@($recordSec[$shift..($recordSec.Count - 1)])
    $blocks = [System.Collections.ArrayList]@($blocks[$shift..($blocks.Count - 1)])
    $particleOf = [System.Collections.ArrayList]@($particleOf[$shift..($particleOf.Count - 1)])
    $minutes[0] = $null
    $checkpoints = @($checkpoints | Where-Object { $_ -gt $StartAt } | ForEach-Object { $_ - $shift })
    $shifted = @{}
    foreach ($k in $changeBefore.Keys) { if ($k -gt $StartAt) { $shifted[$k - $shift] = $changeBefore[$k] } }
    $changeBefore = $shifted
    $guardBaseline = 5.0
    Write-Host "[PAPER] -StartAt ${StartAt}: resuming from run $StartAt of the full list; runs are renumbered from 1."
}

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
$changeNumbers = @($changeBefore.Keys | Sort-Object)
if ($changeNumbers.Count -gt 0) { $recordArgs += @("--particle-change-run-numbers", ($changeNumbers -join ",")) }
if (-not $NoParticlePrompts) {
    # Particle number and caliper diameters asked on screen at the start, at each change and after collection.
    $recordArgs += @("--particle-prompts", "--initial-particle", [string]$particleOf[0])
    if ($changeNumbers.Count -gt 0) {
        $recordArgs += @("--particle-change-labels", (($changeNumbers | ForEach-Object { "{0}:{1}" -f $_, $changeBefore[$_] }) -join ","))
    }
    if ($Calipers -ne "") { $recordArgs += @("--particle-calipers", $Calipers) }
    if ($MeasuredBy -ne "") { $recordArgs += @("--particle-measured-by", $MeasuredBy) }
    if ($ParticleSource -ne "") { $recordArgs += @("--particle-source", $ParticleSource) }
}
if (-not $NoThermalPrompts) {
    # Plan: temperatures every 10 minutes (the sensors fill the fields they cover).
    $recordArgs += @("--thermal-prompt-every", "0:10", "--thermal-prompt-timeout-sec", $ThermalPromptTimeoutSec.ToString($invariant))
}
if ($PatBoardGapMm -ne "") { $recordArgs += @("--pat-board-gap-mm", $PatBoardGapMm) }
if ($KeepFailedCaptures) { $recordArgs += "--keep-failed-captures" }
if ($NoHoldAtEnd) { $recordArgs += "--no-hold-particle-until-enter" }   # the hold is on by default
# After each run: is the particle back at the centre? pause = wait for a new particle and retake the run once.
$recordArgs += @("--particle-check", $ParticleCheck)
# A failed capture (LED, recorder) is re-recorded automatically up to -AutomaticCaptureRetries times; then the
# operator is notified and asked (Y/n) -- unless -Unattended, which stops the session instead.
$recordArgs += @("--automatic-capture-retries", "$AutomaticCaptureRetries")
if (-not $Unattended) { $recordArgs += "--prompt-on-capture-failure" }

# Estimate like the analysis side's summary.md: record length + 65 s per run, 3 min per particle change.
$estimateMin = 0.0
foreach ($r in $recordSec) { $estimateMin += ($r + 65.0) / 60.0 }
$estimateMin += 3.0 * $changeNumbers.Count
Write-Host ("[PAPER] {0}: {1} V, {2} runs, {3} particle change(s), estimate {4:0} min after the warm-up ({5:0} min with -WarmupMin {6}). Output {7}." -f ($sessions -join "+"), $SupplyVoltage, $runs.Count, $changeNumbers.Count, $estimateMin, ($estimateMin + $WarmupMin), $WarmupMin, $OutputDir)
$index = 0
foreach ($label in $runs) {
    $index += 1
    $at = if ($null -eq $minutes[$index - 1]) { "     " } else { "{0,3} m" -f $minutes[$index - 1] }
    $mark = ""
    if ($changeBefore.ContainsKey($index)) { $mark += "  <- change to particle " + $changeBefore[$index] }
    if ($checkpoints -contains $index) { $mark += "  <- confirm" }
    Write-Host ("[PAPER]  {0,3}. {1}  {2,-14} {3}{4}" -f $index, $at, $blocks[$index - 1], $label, $mark)
}

Write-Host "[PAPER] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) { throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened." }
if ($DryRunOnly) {
    Write-Host "[PAPER] -DryRunOnly: hardware was not opened."
    exit 0
}

Write-Host "[PAPER] Check before starting:"
Write-Host "  - Supply at $SupplyVoltage V, cooling fans ON. Particle 0 for S1-S3 (keep it between sessions); new particles where the list says 'change'."
Write-Host "  - Measure each particle with the calipers (3 directions, 0.1 mm) before placing it: the screen asks 'number d1 d2 d3 [note]'"
Write-Host "    at the start and at each change, and the after-use diameters at the change and after the final collection."
Write-Host "    -Calipers, -MeasuredBy, -ParticleSource are kept with every particle; particle_record.py adds or fixes fields later."
Write-Host "  - PAT-start LED bright (kcheck LED peak count 1000 or more), and no glint of the lighting LED inside the left LED ROI."
Write-Host "  - Room temperature, humidity and air pressure are logged from the Tempo Disc (close the bmLogger app; -NoEnvSensor turns it off)."
Write-Host "  - PAT board temperatures are logged from the DS18B20 Arduino (COM12, which also drives the start LED and the lighting strobe; -PatTempPort, default auto; -NoPatTempSensor turns it off). Close pat_temp_app.py first (it holds COM12)."
Write-Host "  - Temperatures are asked on screen every 10 minutes; the sensors fill what they cover. -NoThermalPrompts turns that off."
Write-Host "  - After each run the events are checked for the particle at the centre (-ParticleCheck $ParticleCheck). If it is gone, you are"
Write-Host "    notified and the session waits: put a particle back, confirm it in the preview, and the run is recorded again once."
Write-Host "  - At the end the PAT keeps holding the particle at the centre until you press Enter (collect it first)."
$psuTarget = Get-PsuTargetArgs -Usb:$PsuUsb -Resource $PsuResource -HostName $PsuHost
$psuLogger = Start-PsuLogger -Python $python -OutputDir $OutputDir -TargetArgs $psuTarget -IntervalSec $PsuIntervalSec
$envLogger = $null
if (-not $NoEnvSensor) { $envLogger = Start-EnvLogger -Python $python -OutputDir $OutputDir -Device $EnvDevice }
if ($null -ne $envLogger) { $recordArgs += @("--env-log", $envLogger.Log) }
$patTempLogger = $null
if (-not $NoPatTempSensor) { $patTempLogger = Start-PatTempLogger -Python $python -OutputDir $OutputDir -Port $PatTempPort -SensorMap $PatTempSensorMap }
if ($null -ne $patTempLogger) { $recordArgs += @("--pat-temp-log", $patTempLogger.Log) }
if ($null -ne $psuLogger) { $recordArgs += @("--psu-log", $psuLogger.Log) }
$recordArgs += Get-CurrentGuardArgs -Logger $psuLogger -Percent $CurrentGuardPercent -BaselineMin $guardBaseline -Disabled:$NoCurrentGuard
$recordArgs += Get-NotifyArgs
try {
    & $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs `
        --acknowledge-ff-validation-protocol --acknowledge-step-response-risk
    $code = $LASTEXITCODE
}
finally {
    Stop-EnvLogger $envLogger
    Stop-PatTempLogger $patTempLogger
    Stop-PsuLogger $psuLogger
    Write-PsuReport -Python $python -OutputDir $OutputDir -Logger $psuLogger
}
if ($code -eq 0) { Write-Host "[PAPER] Finished. Output: $OutputDir" }
elseif ($code -eq 3) { Write-Host "[PAPER] The current guard stopped the session before the next run (see current_guard in the session JSON)." }
else { Write-Host "[PAPER] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir." }
exit $code

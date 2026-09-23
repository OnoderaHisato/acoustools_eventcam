<#
.SYNOPSIS
Record the feedforward validation of the 7 mm / 10 Hz heart (OFF / A / C / OT-ident designs) in one PAT session.

.DESCRIPTION
1. Generates the export from ff_heart_20260918\ff_heart_plan.json
   (skipped when export_manifest.json already exists; use -Regenerate).
   The heart commands are imported numerically unchanged from
   ff_heart_20260918\source\*.npz (copies of the G: drive plan files).
2. Runs the hardware-free dry run for the selected sequence.
3. Opens PAT/OpenMPD once and records the sequence. Default (plan proposal):
     OFF -> A -> C -> C -> A -> OFF
   with every heart run sandwiched by the short stiffness checks
   (kcheck x, y and z: 1.05 mm, 6 jumps, 2.0 s holds, 14 s each).
   By default only the FIRST run has a stereo preview and an Enter checkpoint; the rest
   start automatically. A failed capture still asks "re-record? (Y/n)".
   -ConfirmEachRun restores the preview and Enter before every run.
   -Unattended re-records a failed capture automatically instead of asking.
   Particle loss is NOT detected automatically.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -DryRunOnly

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Unattended

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OFF" -NoSandwich -CommandScale 0.5

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "C,OTI"

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OFF5,OFF7"

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OFF1,OFF2"

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OFF1YZ,OFF2YZ"

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "OFF7,OFFW7,C7,CW7"

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "WXZ,WYZ"

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_ff_heart_validation.ps1 -Designs "CWXZ7,CW2"
#>
[CmdletBinding()]
param(
    [string]$OutputDir = ".\stereo_acoustools_3d_records_ff_heart",
    # Order of the heart runs. OFF = u=r, A = lookahead only, C = lookahead + inverse model,
    # OTI (or OT-IDENT) = OptiTrap-type inverse model with identified parameters, no lookahead.
    # A03 / C03 = designs A and C re-tuned to the 0.3 ms delay measured on 2026-09-18.
    # OFF1 / OFF2 / OFF5 / OFF7 / OFF10 = the heart without compensation at 1 / 2 / 5 / 7 / 10 Hz
    # (OFF10 is the same run as OFF; the 1 Hz and 2 Hz commands last 14 s, the others 8 s).
    # OFF1YZ ... OFF10YZ = the same commands drawn in the YZ plane (x and y columns swapped).
    # OFFW7 / CW7 / OFFW10 / CW10 = OFF and C minus the measured w field at 7 / 10 Hz (XZ plane);
    # C7 = design C at 7 Hz. OFFW10 and CW10 are pinned to a 110,000 mm/s^2 acceleration limit.
    # OFFWXZ7 / CWXZ7 = OFFW and CW at 7 Hz with the out-of-plane y left uncompensated;
    # OFFW2 / CW2 = the same with the fine y taken from the resonant response (stage 2 of W_FIELD_3D_PLAN).
    # WXZ / WYZ / WXY / WXZP4 / WXZM4 = the w-field surface scans; each one records that plane's
    # spiral a (counter-clockwise) then b (clockwise), 14 s each, and needs no stiffness checks.
    # K = the stiffness checks (-SandwichAxes) on their own, e.g. "K,WXZ,WYZ,WXY,WXZP4,WXZM4".
    [string]$Designs = "OFF,A,C,C,A,OFF",
    # PAT supply voltage set on the power supply. When given, every run directory ends in
    # _V<voltage>_<time> and records supply_voltage_V (THERMAL_PLAN_20260922.md: never mix voltages).
    [double]$SupplyVoltage = 0.0,
    # Stiffness-check axes recorded before every heart run and once more at the end.
    [string]$SandwichAxes = "x,y,z",
    [switch]$NoSandwich,
    # Scale applied to the heart commands only (0 < scale <= 1). The designs are linear in r,
    # so a scaled command is the same feedforward design for a smaller heart.
    [double]$CommandScale = 1.0,
    # Preview and Enter before every run (the behaviour before 2026-09-20).
    [switch]$ConfirmEachRun,
    # Re-record a failed capture automatically instead of asking.
    [switch]$Unattended,
    [ValidateRange(0, 10)]
    [int]$AutomaticCaptureRetries = 2,
    # 140,000-frame stiffness checks start about 2 s after the camera; keep the tail recorded.
    [double]$CaptureTailMarginSec = 5.0,
    # Keep the files of a failed capture (LED count plot, recorder metadata) for diagnosis.
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
if (-not (Test-Path -LiteralPath $python)) {
    throw "venv Python not found: $python"
}

$campaign = ".\ff_heart_20260918"
$plan = Join-Path $campaign "ff_heart_plan.json"
$export = Join-Path $campaign "export_ff_heart"
if (-not (Test-Path -LiteralPath $plan)) {
    throw "Plan not found: $plan"
}

$designLabels = @{
    "OFF" = "ffheart_s7_f10_OFF"
    "A"   = "ffheart_s7_f10_A_delay"
    "C"   = "ffheart_s7_f10_C_delay_inverse"
    "A03" = "ffheart_A_delay03"
    "C03" = "ffheart_C_delay03_inv_k918"
    "OFF1" = "ffheart_s7_f1_OFF"
    "OFF2" = "ffheart_s7_f2_OFF"
    "OFF5" = "ffheart_s7_f5_OFF"
    "OFF7" = "ffheart_s7_f7_OFF"
    "OFF10" = "ffheart_s7_f10_OFF"
    "OFF1YZ" = "ffheart_s7_f1_OFF_yz"
    "OFF2YZ" = "ffheart_s7_f2_OFF_yz"
    "OFF5YZ" = "ffheart_s7_f5_OFF_yz"
    "OFF7YZ" = "ffheart_s7_f7_OFF_yz"
    "OFF10YZ" = "ffheart_s7_f10_OFF_yz"
    "OFFW7" = "ffheart_s7_f7_OFFW"
    "CW7" = "ffheart_s7_f7_CW"
    "C7" = "ffheart_s7_f7_C_delay_inv"
    "OFFW10" = "ffheart_s7_f10_OFFW"
    "CW10" = "ffheart_s7_f10_CW"
    "OFFWXZ7" = "ffheart_s7_f7_OFFWxz"
    "CWXZ7" = "ffheart_s7_f7_CWxz"
    "OFFW2" = "ffheart_s7_f7_OFFW2"
    "CW2" = "ffheart_s7_f7_CW2"
    "OTI" = "ffheart_OT_ident_nodelay"
    "OT-IDENT" = "ffheart_OT_ident_nodelay"
    "OT_IDENT" = "ffheart_OT_ident_nodelay"
}
# The w-field scans: one design token records the plane's a and b spirals back to back.
$scanLabels = @{
    "WXZ" = @("wscan_XZ_a", "wscan_XZ_b")
    "WYZ" = @("wscan_YZ_a", "wscan_YZ_b")
    "WXY" = @("wscan_XY_a", "wscan_XY_b")
    "WXZP4" = @("wscan_XZyp4_a", "wscan_XZyp4_b")
    "WXZM4" = @("wscan_XZym4_a", "wscan_XZym4_b")
}
$checkLabels = @{
    "x" = "kcheck_x_S105_6jumps"
    "y" = "kcheck_y_S105_6jumps"
    "z" = "kcheck_z_S105_6jumps"
}
# Heart commands that are not 8 s long.
$heartSeconds = @{
    "ffheart_s7_f1_OFF" = 14.0
    "ffheart_s7_f2_OFF" = 14.0
    "ffheart_s7_f1_OFF_yz" = 14.0
    "ffheart_s7_f2_OFF_yz" = 14.0
}

$selectedDesigns = @(
    $Designs.Split(",") | ForEach-Object { $_.Trim().ToUpperInvariant() } | Where-Object { $_ -ne "" }
)
if ($selectedDesigns.Count -eq 0) {
    throw "No designs selected. Use a comma-separated list of OFF, A, C, OTI, A03, C03, OFF1, OFF2, OFF5, OFF7, OFF10, and OFF1YZ, OFF2YZ, OFF5YZ, OFF7YZ, OFF10YZ, OFFW7, CW7, C7, OFFW10, CW10, WXZ, WYZ, WXY, WXZP4, WXZM4, OFFWXZ7, CWXZ7, OFFW2, CW2."
}
foreach ($design in $selectedDesigns) {
    if ($design -eq "K") {
        continue
    }
    if ((-not $designLabels.ContainsKey($design)) -and (-not $scanLabels.ContainsKey($design))) {
        throw "Unknown design '$design'. Use: OFF, A, C, OTI, A03, C03, OFF1, OFF2, OFF5, OFF7, OFF10, and OFF1YZ, OFF2YZ, OFF5YZ, OFF7YZ, OFF10YZ, OFFW7, CW7, C7, OFFW10, CW10, WXZ, WYZ, WXY, WXZP4, WXZM4, OFFWXZ7, CWXZ7, OFFW2, CW2"
    }
}

$selectedAxes = @()
if (-not $NoSandwich) {
    $selectedAxes = @(
        $SandwichAxes.Split(",") | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ -ne "" }
    )
    foreach ($axis in $selectedAxes) {
        if (-not $checkLabels.ContainsKey($axis)) {
            throw "Unknown stiffness-check axis '$axis'. Use: x, y, z"
        }
    }
}

if (($CommandScale -le 0.0) -or ($CommandScale -gt 1.0)) {
    throw "-CommandScale must be in (0, 1]; got $CommandScale"
}
if ($CaptureTailMarginSec -lt 0.0) {
    throw "-CaptureTailMarginSec must not be negative"
}
if ($ConfirmEachRun -and $Unattended) {
    throw "-ConfirmEachRun and -Unattended cannot be combined"
}
$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$scaleText = $CommandScale.ToString($invariant)
$marginText = $CaptureTailMarginSec.ToString($invariant)

$manifest = Join-Path $export "export_manifest.json"
# Regenerate automatically when the plan changed after the export was written (for example a
# design was added). The export is never rewritten while it matches the plan.
$exportIsStale = $false
if (Test-Path -LiteralPath $manifest) {
    $planHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $plan).Hash.ToLowerInvariant()
    $manifestHash = ""
    try {
        $manifestHash = [string]((Get-Content -Raw -Encoding UTF8 -LiteralPath $manifest | ConvertFrom-Json).source_plan_sha256)
    }
    catch {
        $manifestHash = ""
    }
    if ($manifestHash.ToLowerInvariant() -ne $planHash) {
        $exportIsStale = $true
        Write-Host "[FF] The export does not match the current plan; it will be regenerated."
    }
}
if ($Regenerate -or $exportIsStale -or -not (Test-Path -LiteralPath $manifest)) {
    foreach ($name in @("heart_s7_f7_OFFWxz.npz", "heart_s7_f7_CWxz.npz", "heart_s7_f7_OFFW2.npz", "heart_s7_f7_CW2.npz",
                        "wscan_XZ_a.npz", "wscan_XZ_b.npz", "wscan_YZ_a.npz", "wscan_YZ_b.npz", "wscan_XY_a.npz",
                        "wscan_XZyp4_a.npz", "wscan_XZyp4_b.npz", "wscan_XZym4_a.npz", "wscan_XZym4_b.npz", "wscan_XY_b.npz",
                        "heart_s7_f10_OFF.npz", "heart_s7_f10_A_delay.npz", "heart_s7_f10_C_delay_inverse.npz", "heart_s7_f10_OT_ident_nodelay.npz", "heart_s7_f10_A_delay03.npz", "heart_s7_f10_C_delay03_inverse_k918.npz", "heart_s7_f5_OFF.npz", "heart_s7_f7_OFF.npz", "heart_s7_f1_OFF.npz", "heart_s7_f2_OFF.npz", "heart_s7_f7_OFFW.npz", "heart_s7_f7_CW.npz", "heart_s7_f7_C_delay_inverse.npz", "heart_s7_f10_OFFW.npz", "heart_s7_f10_CW.npz")) {
        $source = Join-Path (Join-Path $campaign "source") $name
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Source command not found: $source. Copy it from the G: drive plan folder Experiment/20260918/measurement_plan/ff_heart (see FF_HEART_VALIDATION_MEASUREMENT_JP.md)."
        }
    }
    Write-Host "[FF] Generating $export from $plan"
    & $python .\hf_identification_trajectory.py --plan $plan --output-dir $export
    if ($LASTEXITCODE -ne 0) {
        throw "Export generation failed (exit code $LASTEXITCODE)"
    }
}
else {
    Write-Host "[FF] Using existing $export"
}

# Build the ordered run list: [checks] heart [checks] heart ... [checks].
# A w-field scan needs no stiffness checks, so it is recorded on its own (plan item 2).
$requests = @()
$motionSec = 0.0
$sandwichedAny = $false
foreach ($design in $selectedDesigns) {
    if ($design -eq "K") {
        if ($selectedAxes.Count -eq 0) {
            throw "Design K records the stiffness checks on their own; it cannot be combined with -NoSandwich."
        }
        foreach ($axis in $selectedAxes) {
            $requests += "$($checkLabels[$axis])=1"
            $motionSec += 14.0
        }
        continue
    }
    if ($scanLabels.ContainsKey($design)) {
        foreach ($label in $scanLabels[$design]) {
            $requests += "$label=$scaleText"
            $motionSec += 14.0
        }
        continue
    }
    $sandwichedAny = $true
    foreach ($axis in $selectedAxes) {
        $requests += "$($checkLabels[$axis])=1"
        $motionSec += 14.0
    }
    $heartLabel = $designLabels[$design]
    $requests += "$heartLabel=$scaleText"
    if ($heartSeconds.ContainsKey($heartLabel)) { $motionSec += $heartSeconds[$heartLabel] } else { $motionSec += 8.0 }
}
if ($sandwichedAny) {
    foreach ($axis in $selectedAxes) {
        $requests += "$($checkLabels[$axis])=1"
        $motionSec += 14.0
    }
}

$recordArgs = @(
    "--hf-export-dir", $export,
    "--output-dir", $OutputDir,
    "--capture-tail-margin-sec", $marginText
)
foreach ($request in $requests) {
    $recordArgs += @("--hf-run", $request)
}
if ($KeepFailedCaptures) {
    $recordArgs += "--keep-failed-captures"
}
if ($SupplyVoltage -lt 0.0 -or $SupplyVoltage -gt 30.0) {
    throw "-SupplyVoltage must be in (0, 30] (or omitted); got $SupplyVoltage"
}
if ($SupplyVoltage -gt 0.0) {
    $voltageText = $SupplyVoltage.ToString($invariant)
    if (-not $PSBoundParameters.ContainsKey("OutputDir")) {
        # One folder per voltage, short enough that the _V<voltage> tag is never cut.
        $OutputDir = ".\stereo_acoustools_3d_records_V" + $voltageText.Replace(".", "p")
        $recordArgs[3] = $OutputDir
    }
    $recordArgs += @("--supply-voltage-v", $voltageText)
}
if (-not $ConfirmEachRun) {
    $recordArgs += "--unattended-after-first-checkpoint"
    if ($Unattended) {
        $recordArgs += @("--automatic-capture-retries", "$AutomaticCaptureRetries")
    }
    else {
        $recordArgs += "--prompt-on-capture-failure"
    }
}

Write-Host "[FF] Sequence of $($requests.Count) run(s), PAT motion $motionSec s in total:"
$index = 0
foreach ($request in $requests) {
    $index += 1
    Write-Host ("[FF]   {0,2}. {1}" -f $index, $request)
}

if ($requests.Count -gt 12) {
    Write-Host "[FF] WARNING: $($requests.Count) runs in one PAT session. Long sessions lost the LED after 16-17 minutes on 2026-09-16 (twice); consider splitting -Designs into sessions of 12 runs or fewer."
}

Write-Host "[FF] Hardware-free dry run"
& $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs --dry-run
if ($LASTEXITCODE -ne 0) {
    throw "Dry run failed (exit code $LASTEXITCODE); hardware was not opened."
}
if ($DryRunOnly) {
    Write-Host "[FF] -DryRunOnly: hardware was not opened."
    exit 0
}

$ackArgs = @("--acknowledge-ff-validation-protocol")
if ($selectedAxes.Count -gt 0) {
    $ackArgs += "--acknowledge-step-response-risk"
}
if ($ConfirmEachRun) {
    Write-Host "[FF] Opening PAT and cameras. Every run waits for the stereo preview and Enter; press Ctrl+C there to stop."
}
else {
    Write-Host "[FF] Opening PAT and cameras. Confirm the FIRST run in the stereo preview and press Enter; the rest start automatically."
}
$psuTarget = Get-PsuTargetArgs -Usb:$PsuUsb -Resource $PsuResource -HostName $PsuHost
$psuLogger = Start-PsuLogger -Python $python -OutputDir $OutputDir -TargetArgs $psuTarget -IntervalSec $PsuIntervalSec
if ($null -ne $psuLogger) { $recordArgs += @("--psu-log", $psuLogger.Log) }
try {
    & $python .\acoustools_stereo_eventcam_3d_recording_auto.py @recordArgs @ackArgs
    $code = $LASTEXITCODE
}
finally {
    Stop-PsuLogger $psuLogger
    Write-PsuReport -Python $python -OutputDir $OutputDir -Logger $psuLogger
}
if ($code -eq 0) {
    Write-Host "[FF] All selected runs were recorded. Output: $OutputDir"
}
else {
    Write-Host "[FF] Recording stopped with exit code $code. See the auto_recording_session JSON in $OutputDir."
}
exit $code

# Shared helpers: log the PAT supply (Kikusui PWR801L) during a recording session.
# Dot-source from a batch script:  . (Join-Path $PSScriptRoot "psu_logging.ps1")
#
# The logger (pwr01_logger.py) is read-only: it sends MEAS:ALL?, OUTP?, VOLT?, CURR? and hands
# the panel back to LOCAL after each reading. It is started before PAT opens, so the current
# step at sound on is recorded, and stopped after the session ends (or fails).

function Get-PsuTargetArgs {
    param([switch]$Usb, [string]$Resource = "", [string]$HostName = "")
    $count = @($Usb.IsPresent, ($Resource -ne ""), ($HostName -ne "")) | Where-Object { $_ } | Measure-Object
    if ($count.Count -gt 1) { throw "Use only one of -PsuUsb, -PsuResource, -PsuHost." }
    if ($Usb) { return @("--usb") }
    if ($Resource -ne "") { return @("--resource", $Resource) }
    if ($HostName -ne "") { return @("--host", $HostName) }
    return @()
}

function Start-PsuLogger {
    param(
        [string]$Python,
        [string]$OutputDir,
        [string[]]$TargetArgs,
        [double]$IntervalSec = 1.0
    )
    if ($TargetArgs.Count -eq 0) { return $null }
    Write-Host "[PSU] Connection test (read-only)"
    & $Python .\pwr01_logger.py @TargetArgs --once
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot read the supply ($($TargetArgs -join ' ')). Fix the connection or run without the -Psu option."
    }
    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $log = Join-Path (Resolve-Path $OutputDir) "psu_log_$stamp.csv"
    $stop = Join-Path (Resolve-Path $OutputDir) "psu_log_$stamp.stop"
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    $argList = @(".\pwr01_logger.py") + $TargetArgs + @(
        "--output", "`"$log`"",
        "--interval", $IntervalSec.ToString($invariant),
        "--stop-file", "`"$stop`""
    )
    $process = Start-Process -FilePath (Resolve-Path $Python) -ArgumentList $argList `
        -WorkingDirectory $PSScriptRoot -WindowStyle Minimized -PassThru
    Start-Sleep -Seconds 2
    if ($process.HasExited) {
        throw "The supply logger stopped right away (exit code $($process.ExitCode))."
    }
    Write-Host "[PSU] Logging every $IntervalSec s to $log"
    return [pscustomobject]@{ Process = $process; Log = $log; Stop = $stop }
}

function Write-PsuReport {
    # Figure + summary of the session (steadiness, drops). Never stops the batch script.
    param([string]$Python, [string]$OutputDir, $Logger)
    if ($null -eq $Logger) { return }
    try {
        & $Python .\psu_log_report.py $OutputDir --psu-log $Logger.Log
    }
    catch {
        Write-Host "[PSU][WARN] the supply report could not be written: $_"
    }
}

function Stop-PsuLogger {
    param($Logger)
    if ($null -eq $Logger) { return }
    New-Item -ItemType File -Force -Path $Logger.Stop | Out-Null
    try {
        Wait-Process -Id $Logger.Process.Id -Timeout 15 -ErrorAction Stop
    }
    catch {
        # Over USB the logger runs as a child process (KI-VISA workaround); stop the whole tree.
        & taskkill.exe /PID $Logger.Process.Id /T /F 2>$null | Out-Null
    }
    Remove-Item -LiteralPath $Logger.Stop -ErrorAction SilentlyContinue
    Write-Host "[PSU] Supply log: $($Logger.Log)"
}

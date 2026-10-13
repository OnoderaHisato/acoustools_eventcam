# Shared helpers: log the room environment (Blue Maestro Tempo Disc: temperature, humidity, air pressure)
# during a recording session. Dot-source from a batch script:  . (Join-Path $PSScriptRoot "env_logging.ps1")
#
# tempodisc_logger.py only listens to the sensor's Bluetooth advertisement (no connection), so it cannot
# change the sensor. It is started before PAT opens and stopped after the session ends (or fails). The
# environment is a side record: when the sensor cannot be heard, the session goes on without it (warning).
# Close the bmLogger phone app first; a sensor connected to the phone may stop advertising.

function Start-EnvLogger {
    param(
        [string]$Python,
        [string]$OutputDir,
        [string]$Device = "F9EE9FFC",
        [double]$IntervalSec = 10.0
    )
    Write-Host "[ENV] Looking for the Tempo Disc $Device (Bluetooth, listen only, up to 30 s)"
    # Out-Host: the printed reading must not become part of this function's return value.
    & $Python .\tempodisc_logger.py --device $Device --once --timeout 30 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ENV][WARN] The Tempo Disc was not heard; the session runs WITHOUT the environment log."
        Write-Host "[ENV][WARN] (Bluetooth on? sensor nearby? bmLogger app closed?)  -NoEnvSensor hides this check."
        return $null
    }
    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $log = Join-Path (Resolve-Path $OutputDir) "env_log_$stamp.csv"
    $stop = Join-Path (Resolve-Path $OutputDir) "env_log_$stamp.stop"
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    $argList = @(
        ".\tempodisc_logger.py", "--device", $Device,
        "--output", "`"$log`"",
        "--interval", $IntervalSec.ToString($invariant),
        "--stop-file", "`"$stop`""
    )
    $process = Start-Process -FilePath (Resolve-Path $Python) -ArgumentList $argList `
        -WorkingDirectory $PSScriptRoot -WindowStyle Minimized -PassThru
    Start-Sleep -Seconds 2
    if ($process.HasExited) {
        Write-Host "[ENV][WARN] The environment logger stopped right away (exit code $($process.ExitCode)); running without it."
        return $null
    }
    Write-Host "[ENV] Logging the room temperature, humidity and pressure to $log"
    return [pscustomobject]@{ Process = $process; Log = $log; Stop = $stop }
}

function Stop-EnvLogger {
    param($Logger)
    if ($null -eq $Logger) { return }
    New-Item -ItemType File -Force -Path $Logger.Stop | Out-Null
    try {
        Wait-Process -Id $Logger.Process.Id -Timeout 15 -ErrorAction Stop
    }
    catch {
        & taskkill.exe /PID $Logger.Process.Id /T /F 2>$null | Out-Null
    }
    Remove-Item -LiteralPath $Logger.Stop -ErrorAction SilentlyContinue
    Write-Host "[ENV] Environment log: $($Logger.Log)"
}

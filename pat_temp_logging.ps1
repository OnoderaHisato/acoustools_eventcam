# Shared helpers: log the PAT board temperatures (DS18B20 sensors read by an Arduino over USB serial)
# during a recording session. Dot-source from a batch script:  . (Join-Path $PSScriptRoot "pat_temp_logging.ps1")
#
# pat_temp_logger.py only reads the Arduino (it sends nothing but a bus rescan) and never opens an FTDI
# port, so it cannot touch the PAT controller boards (FTDI FT232H). It is started before PAT opens and
# stopped after the session ends (or fails). The temperatures are a side record: when the Arduino does not
# answer, the session goes on without them (warning).

function Start-PatTempLogger {
    param(
        [string]$Python,
        [string]$OutputDir,
        [string]$Port = "auto",
        [string]$SensorMap = ""
    )
    $mapArgs = @()
    if ($SensorMap -ne "") {
        if (-not (Test-Path -LiteralPath $SensorMap)) { throw "PAT temperature sensor map not found: $SensorMap" }
        $mapArgs = @("--sensor-map", "`"$((Resolve-Path $SensorMap).Path)`"")
    }
    Write-Host "[PATTEMP] Checking the DS18B20 Arduino on port $Port"
    # Out-Host: the printed readings must not become part of this function's return value.
    & $Python .\pat_temp_logger.py --port $Port --once @mapArgs | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[PATTEMP][WARN] No valid PAT temperature reading; the session runs WITHOUT the PAT temperature log."
        Write-Host "[PATTEMP][WARN] Most likely the monitor window (pat_temp_app.py, live mode) still holds COM12: close it and start again."
        Write-Host "[PATTEMP][WARN] Otherwise: Arduino plugged in? sketch uploaded? wiring and 4.7 kOhm pull-up?  -NoPatTempSensor hides this check."
        return $null
    }
    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $log = Join-Path (Resolve-Path $OutputDir) "pat_temp_log_$stamp.csv"
    $stop = Join-Path (Resolve-Path $OutputDir) "pat_temp_log_$stamp.stop"
    $argList = @(
        ".\pat_temp_logger.py", "--port", $Port,
        "--output", "`"$log`"",
        "--stop-file", "`"$stop`""
    ) + $mapArgs
    $process = Start-Process -FilePath (Resolve-Path $Python) -ArgumentList $argList `
        -WorkingDirectory $PSScriptRoot -WindowStyle Minimized -PassThru
    Start-Sleep -Seconds 3
    if ($process.HasExited) {
        Write-Host "[PATTEMP][WARN] The PAT temperature logger stopped right away (exit code $($process.ExitCode)); running without it."
        return $null
    }
    Write-Host "[PATTEMP] Logging the PAT board temperatures to $log"
    return [pscustomobject]@{ Process = $process; Log = $log; Stop = $stop }
}

function Stop-PatTempLogger {
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
    Write-Host "[PATTEMP] PAT temperature log: $($Logger.Log)"
}

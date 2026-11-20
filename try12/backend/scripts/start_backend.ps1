<#
.SYNOPSIS
    Start the nifty-scalper backend before the 09:15 IST open (for Task Scheduler).

.DESCRIPTION
    Starts uvicorn (app.main:app) from the backend folder in the background, unless
    something already listens on the port, then waits for /health and prints the
    session-readiness result of the startup check. Safe to run twice: a second run
    sees the port in use and exits 0 without starting another copy.

    It does not start Postgres or Redis; it only warns when their ports do not answer.
    uvicorn's console output goes to logs\uvicorn-<date>.out.log / .err.log; the
    app's own log stays logs\backend.log.

.EXAMPLE
    pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\start_backend.ps1
#>
param(
    [string]$Python = "python",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [int]$HealthTimeoutSeconds = 180,
    [int]$PostgresPort = 5432,
    [int]$RedisPort = 6379
)

$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent $PSScriptRoot
Set-Location $backend

function Test-Port([string]$h, [int]$p) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($h, $p, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(1000)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch { return $false } finally { $client.Close() }
}

$ist = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, "India Standard Time")
Write-Host ("[{0:yyyy-MM-dd HH:mm} IST] nifty-scalper backend start ({1})" -f $ist, $backend)
if ($ist.DayOfWeek -ne "Saturday" -and $ist.DayOfWeek -ne "Sunday" -and $ist.TimeOfDay -ge [TimeSpan]"09:15:00" -and $ist.TimeOfDay -lt [TimeSpan]"15:30:00") {
    Write-Warning "Starting after 09:15 IST: today's candles before now are missing and the 09:15-10:00 / ORB windows may be lost."
}

if (Test-Port $BindHost $Port) {
    Write-Host "Port $Port already answers: the backend is already running. Nothing started."
    exit 0
}
if (-not (Test-Port "127.0.0.1" $PostgresPort)) { Write-Warning "Postgres does not answer on port $PostgresPort." }
if (-not (Test-Port "127.0.0.1" $RedisPort))    { Write-Warning "Redis does not answer on port $RedisPort." }

$logs = Join-Path $backend "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$stamp = "{0:yyyyMMdd}" -f $ist
$out = Join-Path $logs "uvicorn-$stamp.out.log"
$err = Join-Path $logs "uvicorn-$stamp.err.log"

$proc = Start-Process -FilePath $Python `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", $BindHost, "--port", "$Port") `
    -WorkingDirectory $backend -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $out -RedirectStandardError $err
Set-Content -Path (Join-Path $logs "backend.pid") -Value $proc.Id
Write-Host "Started uvicorn, PID $($proc.Id). Console output: $out"

$deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
$health = $null
while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) {
        Write-Error "uvicorn exited with code $($proc.ExitCode); see $err"
        exit 1
    }
    try {
        $health = Invoke-RestMethod -Uri "http://${BindHost}:$Port/health" -TimeoutSec 5
        break
    } catch { Start-Sleep -Seconds 3 }
}
if ($null -eq $health) {
    Write-Error "/health did not answer within $HealthTimeoutSeconds s; see $err and logs\backend.log"
    exit 1
}

Write-Host ("Health: status={0} redis={1} feed_connected={2} session_valid={3}" -f `
    $health.status, $health.redis, $health.feed_connected, $health.session_valid)
$readiness = $health.readiness
if ($null -eq $readiness -or @($readiness.PSObject.Properties).Count -eq 0) {
    Write-Warning "No session-readiness result yet (the strategy runner may not have started); see logs\backend.log."
} else {
    foreach ($p in $readiness.PSObject.Properties) {
        if ($p.Value.ok) {
            Write-Host "Readiness $($p.Name): OK"
        } else {
            Write-Warning ("Readiness {0}: {1}" -f $p.Name, ($p.Value.warnings -join ", "))
        }
    }
    Write-Host "Details: GET http://${BindHost}:$Port/api/v1/trading/signals/diagnostics (underlyings[].readiness, blockers)"
}
exit 0

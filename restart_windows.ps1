param(
    [int]$Port = 8015,
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$url = "http://127.0.0.1:$Port/kanban.html"
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
$processIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)

if ($processIds.Count -gt 0) {
    Write-Host "Stopping Redmine Kanban PID(s): $($processIds -join ', ')"
    Stop-Process -Id $processIds -Force

    for ($i = 0; $i -lt 20; $i++) {
        $remaining = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $remaining) {
            break
        }
        Start-Sleep -Milliseconds 500
    }
} else {
    Write-Host "No Redmine Kanban listener found on port $Port."
}

$remainingAfterStop = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($remainingAfterStop) {
    $remainingIds = @($remainingAfterStop | Select-Object -ExpandProperty OwningProcess -Unique)
    throw "Port $Port is still in use by PID(s): $($remainingIds -join ', ')"
}

$arguments = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-WindowStyle",
    "Hidden",
    "-File",
    (Join-Path $PSScriptRoot "run_windows.ps1"),
    "--serve",
    "--port",
    "$Port"
)

if ($OpenBrowser) {
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        (Join-Path $PSScriptRoot "run_windows.ps1"),
        "-OpenBrowser",
        "--serve",
        "--port",
        "$Port"
    )
}

Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WindowStyle Hidden
Write-Host "Redmine Kanban restarted in the background."
Write-Host "URL: $url"

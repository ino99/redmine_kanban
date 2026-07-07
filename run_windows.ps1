param(
    [switch]$OpenBrowser,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArgs
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Find-SystemPython {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        return @{
            Command = "py"
            Args = @("-3")
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @{
            Command = "python"
            Args = @()
        }
    }

    throw "Python was not found. Install Python 3.10 or later, then run this script again."
}

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    $systemPython = Find-SystemPython
    Write-Host "Creating virtual environment in .venv ..."
    & $systemPython.Command @($systemPython.Args) -m venv .venv
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

$requirements = Get-Content -LiteralPath "requirements.txt" |
    Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") }
if ($requirements) {
    Write-Host "Installing Python requirements ..."
    & $python -m pip install -r requirements.txt
} else {
    Write-Host "No external Python requirements to install."
}

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "Created .env from .env.example. Edit .env before connecting to Redmine."
}

if (-not $AppArgs -or $AppArgs.Count -eq 0) {
    $AppArgs = @("--serve", "--port", "8015")
}

$port = "8015"
for ($i = 0; $i -lt $AppArgs.Count; $i++) {
    if ($AppArgs[$i] -eq "--port" -and ($i + 1) -lt $AppArgs.Count) {
        $port = $AppArgs[$i + 1]
        break
    }
}

$url = "http://127.0.0.1:$port/kanban.html"

if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host ""
    Write-Host "Redmine Kanban is already running on $url."
    if ($OpenBrowser) {
        Start-Process $url
    }
    exit 0
}

if ($OpenBrowser) {
    $helperCommand = @"
`$url = '$url'
for (`$i = 0; `$i -lt 30; `$i++) {
    try {
        Invoke-WebRequest -Uri `$url -UseBasicParsing -TimeoutSec 2 | Out-Null
        Start-Process `$url
        exit 0
    } catch {
        Start-Sleep -Seconds 1
    }
}
Start-Process `$url
"@
    Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-Command",
        $helperCommand
    ) -WindowStyle Hidden
}

Write-Host ""
Write-Host "Starting Redmine Kanban with Windows Python."
Write-Host "Open $url in your browser."
Write-Host "Press Ctrl+C here to stop the server."
Write-Host ""

& $python redmine_issues.py @AppArgs

param(
    [string]$TaskName = "Redmine Kanban Board"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$scriptPath = Join-Path $PSScriptRoot "run_windows.ps1"
$argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -OpenBrowser"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit 0 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Start the local Redmine Kanban server and open the browser at Windows logon." `
    -Force | Out-Null

Write-Host "Registered startup task: $TaskName"
Write-Host "It will start at Windows logon and open http://127.0.0.1:8015/kanban.html"

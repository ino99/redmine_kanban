param(
    [string]$TaskName = "Redmine Kanban Board"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$launcherPath = Join-Path $PSScriptRoot "run_windows_hidden.vbs"
$argument = "//NoLogo `"$launcherPath`""

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument $argument -WorkingDirectory $PSScriptRoot
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

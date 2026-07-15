# setup_autorun.ps1
# Creates a Windows Task Scheduler job that starts the MT5 bot automatically
# when you log in to Windows. The bot loops every 5 minutes on its own.
#
# Run once as Administrator (or with admin rights):
#   Right-click -> Run with PowerShell
#
# To remove:  Unregister-ScheduledTask -TaskName "EURUSDMTBot" -Confirm:$false
# To start now: Start-ScheduledTask -TaskName "EURUSDMTBot"
# To stop now:  Stop-ScheduledTask  -TaskName "EURUSDMTBot"

$ErrorActionPreference = "Stop"
$taskName = "EURUSDMTBot"
$botDir   = $PSScriptRoot
if (-not $botDir) { $botDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }

# Find python.exe
$python = (Get-Command python -ErrorAction SilentlyContinue)?.Source
if (-not $python) { $python = "python" }

Write-Host ""
Write-Host "Setting up Windows Task Scheduler for EUR/USD MT5 Bot..." -ForegroundColor Cyan
Write-Host "  Bot directory : $botDir" -ForegroundColor Gray
Write-Host "  Python        : $python" -ForegroundColor Gray
Write-Host ""

# Remove existing task if present
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# Action: run run_local.py in the bot directory
$action = New-ScheduledTaskAction `
    -Execute  $python `
    -Argument "run_local.py" `
    -WorkingDirectory $botDir

# Trigger: start when user logs on
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Settings: restart up to 10 times if it crashes; no time limit; one instance only
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount          10 `
    -RestartInterval       (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit    ([System.TimeSpan]::Zero) `
    -MultipleInstances     IgnoreNew `
    -StartWhenAvailable    $true

Register-ScheduledTask `
    -TaskName    $taskName `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -RunLevel    Highest `
    -Force `
    -Description "EUR/USD MT5 Signal Bot — auto-starts on Windows login" | Out-Null

Write-Host "Task '$taskName' registered successfully." -ForegroundColor Green
Write-Host ""
Write-Host "The bot will start automatically on next login." -ForegroundColor Cyan
Write-Host "To start it right now (MT5 must be open first):" -ForegroundColor Yellow
Write-Host "  Start-ScheduledTask -TaskName '$taskName'" -ForegroundColor White
Write-Host ""
Write-Host "Docker dashboard (run separately):" -ForegroundColor Yellow
Write-Host "  docker compose up -d" -ForegroundColor White
Write-Host "  Then open http://localhost:8080" -ForegroundColor White
Write-Host ""
Read-Host "Press Enter to exit"

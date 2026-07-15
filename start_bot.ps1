# start_bot.ps1 — Launch MT5, open dashboard in Chrome, run the bot
# Double-click this file (or run from PowerShell) to start everything.

$ErrorActionPreference = "Continue"
$BotDir = $PSScriptRoot
if (-not $BotDir) { $BotDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }
Set-Location $BotDir

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "   EUR/USD MT5 Bot  --  Launcher                " -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# ── Load credentials from user environment ────────────────────────────────────
$env:MT5_LOGIN      = [System.Environment]::GetEnvironmentVariable("MT5_LOGIN",    "User")
$env:MT5_PASSWORD   = [System.Environment]::GetEnvironmentVariable("MT5_PASSWORD", "User")
$env:MT5_SERVER     = [System.Environment]::GetEnvironmentVariable("MT5_SERVER",   "User")
$env:EXECUTION_MODE = "mt5"

if (-not $env:MT5_LOGIN) {
    Write-Host "ERROR: MT5 credentials not found." -ForegroundColor Red
    Write-Host "Run install.ps1 first to set them up."
    Read-Host "`nPress Enter to exit"
    exit 1
}
Write-Host "  Credentials loaded: Login=$($env:MT5_LOGIN)  Server=$($env:MT5_SERVER)" -ForegroundColor Gray

# ── 1. Check / start MT5 terminal ────────────────────────────────────────────
Write-Host ""
Write-Host "[1/3] Checking MetaTrader 5..." -ForegroundColor Yellow

$mt5Proc = Get-Process "terminal64" -ErrorAction SilentlyContinue

if ($mt5Proc) {
    Write-Host "  MT5 is already running (PID $($mt5Proc[0].Id))." -ForegroundColor Green
} else {
    Write-Host "  MT5 not running. Searching for executable..." -ForegroundColor Yellow

    # Build search list: standard paths + any MT5 variant in Program Files
    $searchPaths = [System.Collections.Generic.List[string]]@(
        "$env:ProgramFiles\MetaTrader 5\terminal64.exe",
        "${env:ProgramFiles(x86)}\MetaTrader 5\terminal64.exe",
        "$env:LOCALAPPDATA\Programs\MetaTrader 5\terminal64.exe"
    )
    # Broker-branded installs (e.g. "ICMarkets MT5", "Pepperstone MT5")
    $brokerExes = Get-ChildItem "$env:ProgramFiles" -Filter "terminal64.exe" `
                      -Recurse -Depth 3 -ErrorAction SilentlyContinue |
                  Select-Object -First 5 -ExpandProperty FullName
    foreach ($p in $brokerExes) { $searchPaths.Add($p) }

    $mt5Exe = $searchPaths | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

    if ($mt5Exe) {
        Write-Host "  Found: $mt5Exe" -ForegroundColor Cyan
        Write-Host "  Starting MT5..." -ForegroundColor Yellow
        Start-Process $mt5Exe
        Write-Host "  Waiting 20 s for MT5 to initialise and auto-login..." -ForegroundColor Gray
        Start-Sleep -Seconds 20
        Write-Host "  MT5 started. If a login dialog appeared, complete it now." -ForegroundColor Yellow
        $resume = Read-Host "  Press Enter when MT5 is logged in and ready"
    } else {
        Write-Host "  MT5 executable not found automatically." -ForegroundColor Red
        Write-Host "  Please open MT5 manually, log in, then press Enter."
        Read-Host
    }
}

# ── 2. Open dashboard in Chrome ───────────────────────────────────────────────
Write-Host ""
Write-Host "[2/3] Opening dashboard..." -ForegroundColor Yellow

$chromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1

# Try to build GitHub Pages URL from git remote
$dashUrl = $null
try {
    $remote = git remote get-url origin 2>$null
    if ($remote -match "github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$") {
        $ghUser = $Matches[1]; $ghRepo = $Matches[2]
        $dashUrl = "https://$ghUser.github.io/$ghRepo/"
        Write-Host "  GitHub Pages URL: $dashUrl" -ForegroundColor Cyan
    }
} catch {}

if (-not $dashUrl) {
    # No GitHub remote: spin up a local HTTP server so fetch() works in Chrome
    Write-Host "  No GitHub remote — starting local server on http://localhost:8080" -ForegroundColor Gray
    $dashJob = Start-Job -Name "DashServer" -ScriptBlock {
        param($d)
        python -m http.server 8080 --directory "$d\docs"
    } -ArgumentList $BotDir
    Start-Sleep -Seconds 2
    $dashUrl = "http://localhost:8080"
    Write-Host "  Local dashboard: $dashUrl" -ForegroundColor Cyan
}

if ($chrome) {
    Start-Process $chrome "--new-window `"$dashUrl`""
} else {
    # Fall back to system default browser
    Start-Process $dashUrl
}
Write-Host "  Dashboard opened." -ForegroundColor Green

# ── 3. Start the bot ──────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[3/3] Starting bot  (Ctrl+C to stop)" -ForegroundColor Green
Write-Host "  Pairs : EUR/USD, GBP/USD" -ForegroundColor Gray
Write-Host "  Cycle : 5 minutes" -ForegroundColor Gray
Write-Host "  Mode  : MT5 demo  (real orders on demo account)" -ForegroundColor Gray
Write-Host ""

python run_local.py

# ── Cleanup ───────────────────────────────────────────────────────────────────
if (Get-Job -Name "DashServer" -ErrorAction SilentlyContinue) {
    Stop-Job  -Name "DashServer" -ErrorAction SilentlyContinue
    Remove-Job -Name "DashServer" -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Bot stopped." -ForegroundColor Yellow
Read-Host "Press Enter to exit"

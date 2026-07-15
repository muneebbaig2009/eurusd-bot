# install.ps1 — One-time setup for the EUR/USD MT5 bot
# Run this once on any new machine before starting the bot.
# PowerShell 5.1+  (pre-installed on Windows 10/11)

$ErrorActionPreference = "Continue"
$BotDir = $PSScriptRoot
if (-not $BotDir) { $BotDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }
Set-Location $BotDir

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "   EUR/USD MT5 Bot  --  Installer               " -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. Python check ───────────────────────────────────────────────────────────
Write-Host "[1/4] Checking Python..." -ForegroundColor Yellow
$pyPath = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyPath) {
    Write-Host "  ERROR: Python not found." -ForegroundColor Red
    Write-Host "  Install Python 3.11+ from https://python.org (tick 'Add to PATH')"
    Read-Host "`nPress Enter to exit"
    exit 1
}
$pyVer = python --version 2>&1
Write-Host "  OK: $pyVer" -ForegroundColor Green

# ── 2. Install packages ───────────────────────────────────────────────────────
Write-Host ""
Write-Host "[2/4] Installing Python packages..." -ForegroundColor Yellow
pip install -r "$BotDir\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARNING: pip reported errors. Some packages may be missing." -ForegroundColor Yellow
} else {
    Write-Host "  OK: all packages installed." -ForegroundColor Green
}

# ── 3. MT5 credentials ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[3/4] MT5 Demo Account Credentials" -ForegroundColor Yellow
Write-Host "  Open MT5: File -> Open an Account  (or read the title bar)" -ForegroundColor Gray
Write-Host ""

# Show existing values if present
$xLogin  = [System.Environment]::GetEnvironmentVariable("MT5_LOGIN",  "User")
$xServer = [System.Environment]::GetEnvironmentVariable("MT5_SERVER", "User")
if ($xLogin) {
    Write-Host "  Existing saved credentials:" -ForegroundColor Gray
    Write-Host "    Login  = $xLogin" -ForegroundColor Gray
    Write-Host "    Server = $xServer" -ForegroundColor Gray
    $skip = Read-Host "  Keep existing credentials? [Y/n]"
    if ($skip.Trim().ToLower() -ne "n") {
        Write-Host "  Keeping existing credentials." -ForegroundColor Green
        $login  = $xLogin
        $server = $xServer
        $pass   = [System.Environment]::GetEnvironmentVariable("MT5_PASSWORD", "User")
        $skipCreds = $true
    }
}

if (-not $skipCreds) {
    $login  = Read-Host "  MT5 Login (account number)"
    $server = Read-Host "  MT5 Server  (e.g. MetaQuotes-Demo)"
    $passSecure = Read-Host "  MT5 Password" -AsSecureString
    $pass = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                [Runtime.InteropServices.Marshal]::SecureStringToBSTR($passSecure))

    [System.Environment]::SetEnvironmentVariable("MT5_LOGIN",    $login,  "User")
    [System.Environment]::SetEnvironmentVariable("MT5_SERVER",   $server, "User")
    [System.Environment]::SetEnvironmentVariable("MT5_PASSWORD", $pass,   "User")
    Write-Host "  Credentials saved to Windows user environment." -ForegroundColor Green
}

# Load into this session
$env:MT5_LOGIN      = $login
$env:MT5_SERVER     = $server
$env:MT5_PASSWORD   = $pass
$env:EXECUTION_MODE = "mt5"

# ── 4. Connection test ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[4/4] Testing MT5 connection..." -ForegroundColor Yellow
Write-Host "  Make sure MT5 terminal is open and logged in." -ForegroundColor Gray
Write-Host ""

$testResult = python -c "
import mt5_executor
try:
    mt5_executor.connect()
    mt5_executor.disconnect()
    print('PASS')
except Exception as e:
    print('FAIL:', e)
" 2>&1

if ($testResult -match "PASS") {
    Write-Host "  Connection test: PASSED" -ForegroundColor Green
} else {
    Write-Host "  Connection test: FAILED" -ForegroundColor Red
    Write-Host "  $testResult"
    Write-Host ""
    Write-Host "  Troubleshooting:" -ForegroundColor Yellow
    Write-Host "   - Is MetaTrader 5 open and logged in?"
    Write-Host "   - Is 'Algo Trading' button green (enabled) in MT5 toolbar?"
    Write-Host "   - Do login/password/server match exactly what MT5 shows?"
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "   Setup complete!                              " -ForegroundColor Green
Write-Host "   Next steps:                                  " -ForegroundColor Green
Write-Host "    1. Run backtest:  python mt5_backtest.py    " -ForegroundColor Green
Write-Host "    2. Start bot:     .\start_bot.ps1           " -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Read-Host "Press Enter to exit"

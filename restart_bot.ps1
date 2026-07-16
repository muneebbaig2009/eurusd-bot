$ErrorActionPreference = "SilentlyContinue"

# Paths
$BOT_DIR   = "e:\eurusd-bot"
$PYTHON    = "$BOT_DIR\.venv\Scripts\python.exe"
$TIMESTAMP = Get-Date -Format "yyyyMMdd_HHmmss"
$LOG_OUT   = "$BOT_DIR\logs\bot_$TIMESTAMP.txt"
$LOG_ERR   = "$BOT_DIR\logs\bot_${TIMESTAMP}_err.txt"

function Divider { Write-Host ("  " + ("-" * 54)) -ForegroundColor DarkGray }

function Row {
    param($icon, $label, $msg, $color = "Green")
    Write-Host ("  {0}  {1,-20} {2}" -f $icon, $label, $msg) -ForegroundColor $color
}
function OK   { param($l,$m) Row "[ OK ]" $l $m "Green"  }
function FAIL { param($l,$m) Row " [!!] " $l $m "Red"    }
function WARN { param($l,$m) Row " [??] " $l $m "Yellow" }
function INFO { param($msg)  Write-Host "        $msg" -ForegroundColor DarkGray }

# Header
Clear-Host
Write-Host ""
Write-Host "  =====================================================" -ForegroundColor Cyan
Write-Host "      EUR/USD BOT  -  RESTART " -NoNewline -ForegroundColor Cyan
Write-Host "& STATUS REPORT" -ForegroundColor Cyan
Write-Host "  =====================================================" -ForegroundColor Cyan
Write-Host ("  " + (Get-Date -Format "yyyy-MM-dd  HH:mm:ss")) -ForegroundColor DarkGray
Write-Host ""

# ── 1. Kill existing ─────────────────────────────────────────────────────────
Write-Host "  [1/5]  Stopping existing bot processes..." -ForegroundColor White
$killed = 0
Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
    if ($cmdline -like "*run_local*" -or $cmdline -like "*eurusd*") {
        Stop-Process -Id $_.Id -Force
        INFO "Killed PID $($_.Id)  (bot process)"
        $killed++
    }
}
if ($killed -eq 0) {
    $remaining = Get-Process python -ErrorAction SilentlyContinue
    if ($remaining) {
        $remaining | ForEach-Object {
            Stop-Process -Id $_.Id -Force
            INFO "Killed PID $($_.Id)  (python)"
            $killed++
        }
    }
}
if ($killed -eq 0) { INFO "No running bot processes found" }
else               { INFO "$killed process(es) stopped -- waiting 3s..."; Start-Sleep -Seconds 3 }

# ── 2. MT5 check ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  [2/5]  Checking MT5 terminal..." -ForegroundColor White
$mt5Proc = Get-Process terminal64 -ErrorAction SilentlyContinue
if ($mt5Proc) { INFO "MT5 is running  (PID $($mt5Proc.Id))" }
else          { INFO "MT5 not detected -- bot will start but data may fail" }

# ── 3. Docker check ──────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  [3/5]  Checking Docker..." -ForegroundColor White
$dashLine = docker ps --format "{{.Names}} {{.Status}}" 2>$null | Select-String "eurusd-dashboard"
if ($dashLine) {
    INFO "eurusd-dashboard  $($dashLine.ToString() -replace 'eurusd-dashboard ', '')"
} else {
    INFO "eurusd-dashboard not running -- attempting start..."
    docker start eurusd-dashboard 2>$null | Out-Null
    Start-Sleep -Seconds 3
}

# ── 4. Start bot ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  [4/5]  Starting bot..." -ForegroundColor White
if (-not (Test-Path "$BOT_DIR\logs")) { New-Item -ItemType Directory -Path "$BOT_DIR\logs" -Force | Out-Null }

$proc = Start-Process `
    -FilePath      $PYTHON `
    -ArgumentList  "run_local.py" `
    -WorkingDirectory $BOT_DIR `
    -RedirectStandardOutput $LOG_OUT `
    -RedirectStandardError  $LOG_ERR `
    -WindowStyle Hidden `
    -PassThru

if (-not $proc) {
    FAIL "Bot" "FAILED TO START"
    Read-Host "`n  Press Enter to exit"
    exit 1
}
INFO "Bot started  --  PID $($proc.Id)"

# ── 5. Wait for first cycle ──────────────────────────────────────────────────
Write-Host ""
Write-Host "  [5/5]  Waiting for first cycle (up to 90s)..." -ForegroundColor White
$jsonPath = "$BOT_DIR\docs\data_eurusd.json"
$waited   = 0
$cycleOk  = $false

while ($waited -lt 90) {
    Start-Sleep -Seconds 5
    $waited += 5
    if (Test-Path $jsonPath) {
        try {
            $j   = Get-Content $jsonPath -Raw | ConvertFrom-Json
            $age = ([System.DateTimeOffset]::UtcNow - [System.DateTimeOffset]::Parse($j.updated_at)).TotalSeconds
            if ($age -lt 90) { $cycleOk = $true; break }
        } catch {}
    }
    INFO "Waiting...  ${waited}s elapsed"
}

if ($cycleOk) { INFO "First cycle done  (${waited}s)" }
else          { INFO "Cycle not confirmed yet -- bot still initialising" }

# ── Status Report ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  STATUS REPORT" -ForegroundColor White
Divider

# Bot process
if ((Get-Process | Where-Object { $_.Id -eq $proc.Id }) -ne $null) {
    OK "Bot Process" "PID $($proc.Id)  running"
} else {
    FAIL "Bot Process" "NOT RUNNING"
}

# Docker
$dashLine = docker ps --format "{{.Names}} {{.Status}}" 2>$null | Select-String "eurusd-dashboard"
if ($dashLine) { OK "Docker" $dashLine.ToString() -replace "eurusd-dashboard ", "" }
else           { FAIL "Docker" "eurusd-dashboard not running" }

# MT5 terminal
$mt5Proc = Get-Process terminal64 -ErrorAction SilentlyContinue
if ($mt5Proc) { OK "MT5 Terminal" "PID $($mt5Proc.Id)  running" }
else          { WARN "MT5 Terminal" "Not detected" }

# JSON-based live data
if (Test-Path $jsonPath) {
    try {
        $eur = Get-Content $jsonPath                         -Raw | ConvertFrom-Json
        $gbp = Get-Content "$BOT_DIR\docs\data_gbpusd.json" -Raw | ConvertFrom-Json

        # MT5 account
        if ($eur.mt5_account.balance) {
            OK "MT5 Account" "#$($eur.mt5_account.login)  |  Balance `$$("{0:N2}" -f $eur.mt5_account.balance)"
        } else {
            WARN "MT5 Account" "Not available yet"
        }

        # Signal pairs
        foreach ($pair in @(@{name="EUR/USD"; d=$eur}, @{name="GBP/USD"; d=$gbp})) {
            $res = $pair.d.cycle_status.signal_result
            $px  = $pair.d.cycle_status.current_price
            $msg = "$res  |  price $px"
            if ($res -eq "data_error" -or $res -eq "order_failed") { FAIL $pair.name $msg }
            else { OK $pair.name $msg }
        }

        # JSON freshness
        $ageMin = [math]::Round(([System.DateTimeOffset]::UtcNow - [System.DateTimeOffset]::Parse($eur.updated_at)).TotalMinutes, 1)
        if ($ageMin -lt 10) { OK   "Dashboard JSON" "Updated $ageMin min ago" }
        else                { WARN "Dashboard JSON" "Last update $ageMin min ago  (may be stale)" }

        # Battery
        $bat = $eur.laptop_battery
        if ($null -ne $bat.percent) {
            $chg    = if ($bat.charging) { "Charging" } else { "On Battery" }
            $batMsg = "$($bat.percent)%  --  $chg"
            if     ($bat.percent -le 20 -and -not $bat.charging) { FAIL "Battery" $batMsg }
            elseif ($bat.percent -le 50)                         { WARN "Battery" $batMsg }
            else                                                 { OK   "Battery" $batMsg }
        }

    } catch {
        WARN "JSON Read" "Parse error: $_"
    }
} else {
    WARN "Data JSON" "Not found -- first cycle may still be running"
}

# Error log
$errLines = Get-Content $LOG_ERR -ErrorAction SilentlyContinue
$hasErr   = $errLines | Select-String "Traceback|RuntimeError|Exception" | Select-Object -First 1
if ($hasErr) { WARN "Error Log" "Errors present -- see $LOG_ERR" }
else         { OK   "Error Log" "Clean" }

Divider

# Final verdict
Write-Host ""
$botAlive = (Get-Process | Where-Object { $_.Id -eq $proc.Id }) -ne $null
if ($botAlive -and $cycleOk) {
    Write-Host "  All systems operational.  Next cycle in ~5 min." -ForegroundColor Green
} elseif ($botAlive) {
    Write-Host "  Bot running -- first cycle pending." -ForegroundColor Yellow
} else {
    Write-Host "  WARNING: Bot not running. Review errors above." -ForegroundColor Red
}

Write-Host ""
Write-Host "  Log files:" -ForegroundColor DarkGray
Write-Host "    $LOG_OUT" -ForegroundColor DarkGray
Write-Host "    $LOG_ERR" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  =====================================================" -ForegroundColor Cyan
Write-Host ""
Read-Host "  Press Enter to close"

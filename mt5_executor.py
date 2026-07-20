"""MT5 order execution — DEMO accounts only.

Safety guarantee: connect() reads account_info().trade_mode and raises if it
detects a real-money account (ACCOUNT_TRADE_MODE_REAL == 2). This check cannot
be disabled without modifying source; it runs on every startup.
"""
import MetaTrader5 as mt5
import config


def connect() -> None:
    """Initialise MT5 terminal, login, and assert demo-only mode."""
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")

    # Only call login() when credentials are explicitly configured.
    # If MT5 terminal is already open and logged in, initialize() alone is enough.
    if config.MT5_LOGIN:
        ok = mt5.login(
            config.MT5_LOGIN,
            password=config.MT5_PASSWORD,
            server=config.MT5_SERVER,
        )
        if not ok:
            mt5.shutdown()
            raise RuntimeError(f"MT5 login failed for #{config.MT5_LOGIN}: {mt5.last_error()}")

    info = mt5.account_info()
    if info is None:
        mt5.shutdown()
        raise RuntimeError("MT5: account_info() returned None after login")

    # ── SAFETY CHECK ──────────────────────────────────────────────────────────
    # trade_mode values: 0=DEMO, 1=CONTEST, 2=REAL
    # We allow 0 and 1; refuse 2 unconditionally.
    if info.trade_mode == mt5.ACCOUNT_TRADE_MODE_REAL:
        mt5.shutdown()
        raise RuntimeError(
            "SAFETY BLOCK: real-money account detected "
            f"(login={info.login}, server={info.server}). "
            "This bot is demo-only. Switch to a demo account or set EXECUTION_MODE=sim."
        )
    # ──────────────────────────────────────────────────────────────────────────

    mode_label = "DEMO" if info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "CONTEST"
    print(
        f"[MT5] Connected  #{info.login}  server={info.server}  "
        f"mode={mode_label}  balance={info.balance:.2f} {info.currency}"
    )


def disconnect() -> None:
    mt5.shutdown()
    print("[MT5] Disconnected")


def _sym(symbol: str) -> str:
    return symbol.replace("/", "")


def _fill_mode(sym: str) -> int:
    """Return the first supported ORDER_FILLING_* mode for this symbol."""
    info = mt5.symbol_info(sym)
    if info is None:
        return mt5.ORDER_FILLING_FOK
    fm = info.filling_mode
    if fm & 4:
        return mt5.ORDER_FILLING_RETURN
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_FOK


def open_trade(symbol: str, direction: str, lot: float,
               sl: float, tp: float, magic: int) -> int | None:
    """Send a market order with SL and TP already set.

    Returns the MT5 position ticket (int) on success, or None on failure.
    The ticket is used to track the position in subsequent cycles.
    """
    sym = _sym(symbol)

    if not mt5.symbol_select(sym, True):
        print(f"[MT5] symbol_select({sym}) failed: {mt5.last_error()}")
        return None

    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        print(f"[MT5] No tick for {sym}: {mt5.last_error()}")
        return None

    if direction == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       sym,
        "volume":       float(lot),
        "type":         order_type,
        "price":        price,
        "sl":           round(sl, 5),
        "tp":           round(tp, 5),
        "deviation":    20,                     # max slippage in points
        "magic":        magic,
        "comment":      f"eurusd-bot #{magic}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": _fill_mode(sym),
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result else "None"
        hint = ""
        if code == 10027:
            hint = " → Enable AutoTrading in MT5 terminal toolbar (green robot icon)"
        elif code == 10018:
            hint = " → Market is closed (weekend or outside trading hours)"
        elif code == 10019:
            hint = " → Insufficient funds"
        print(f"[MT5] order_send failed: retcode={code}{hint}  {mt5.last_error()}")
        return None

    print(
        f"[MT5] Order placed: ticket={result.order}  "
        f"{direction} {lot} lots  SL={sl:.5f}  TP={tp:.5f}"
    )
    return result.order  # position ticket (same as order ticket for market orders)


def get_position_result(ticket: int):
    """Return (status, close_price, close_time) if the position is closed, else None.

    close_time is the UTC datetime of the actual MT5 deal — used as closed_at
    so cooldown starts from when the trade actually closed, not detection time.
    """
    from datetime import datetime, timezone
    # Still open?
    positions = mt5.positions_get(ticket=ticket)
    if positions is not None and len(positions) > 0:
        return None

    # Closed — find the closing deal
    deals = mt5.history_deals_get(position=ticket)
    if deals is None or len(deals) == 0:
        return None

    for deal in deals:
        if deal.entry == mt5.DEAL_ENTRY_OUT:
            status     = "WIN" if deal.profit > 0 else "LOSS"
            close_time = datetime.fromtimestamp(deal.time, tz=timezone.utc)
            return status, deal.price, close_time

    return None


def close_position(ticket: int) -> bool:
    """Close an open position by ticket using an opposite market order."""
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        print(f"[MT5] No open position with ticket {ticket}")
        return False

    pos  = positions[0]
    sym  = pos.symbol

    if not mt5.symbol_select(sym, True):
        print(f"[MT5] symbol_select({sym}) failed: {mt5.last_error()}")
        return False

    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        print(f"[MT5] No tick for {sym}: {mt5.last_error()}")
        return False

    if pos.type == mt5.ORDER_TYPE_BUY:
        order_type, price = mt5.ORDER_TYPE_SELL, tick.bid
    else:
        order_type, price = mt5.ORDER_TYPE_BUY,  tick.ask

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       sym,
        "volume":       float(pos.volume),
        "type":         order_type,
        "position":     ticket,
        "price":        price,
        "deviation":    20,
        "magic":        pos.magic,
        "comment":      f"eurusd-bot close #{ticket}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": _fill_mode(sym),
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result else "None"
        print(f"[MT5] Close failed: retcode={code}  {mt5.last_error()}")
        return False

    print(f"[MT5] Position {ticket} closed  price={price:.5f}")
    return True


def modify_position(ticket: int, sl: float, tp: float) -> bool:
    """Modify the SL and TP of an open position."""
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        print(f"[MT5] No open position with ticket {ticket}")
        return False

    pos = positions[0]
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol":   pos.symbol,
        "sl":       round(sl, 5),
        "tp":       round(tp, 5),
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result else "None"
        print(f"[MT5] modify_position failed: retcode={code}  {mt5.last_error()}")
        return False

    print(f"[MT5] Position {ticket} modified  SL={sl:.5f}  TP={tp:.5f}")
    return True


def get_account_info() -> dict:
    """Return live MT5 account state as a plain dict."""
    info = mt5.account_info()
    if info is None:
        return {}
    return {
        "login":       info.login,
        "balance":     round(info.balance, 2),
        "equity":      round(info.equity, 2),
        "profit":      round(info.profit, 2),
        "margin":      round(info.margin, 2),
        "free_margin": round(info.margin_free, 2),
        "currency":    info.currency,
        "leverage":    info.leverage,
        "server":      info.server,
    }


def get_trade_history(days: int = 60) -> list:
    """Return closed deals placed by this bot over the last N days."""
    from datetime import datetime, timedelta, timezone
    from_dt = datetime.now(timezone.utc) - timedelta(days=days)
    to_dt   = datetime.now(timezone.utc)

    deals = mt5.history_deals_get(from_dt, to_dt)
    if deals is None:
        return []

    trades = []
    for d in deals:
        if "eurusd-bot" not in str(d.comment).lower():
            continue
        if d.entry != mt5.DEAL_ENTRY_OUT:
            continue
        trades.append({
            "ticket":  d.order,
            "time":    datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
            "symbol":  d.symbol,
            "type":    "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
            "volume":  d.volume,
            "price":   round(d.price, 5),
            "profit":  round(d.profit, 2),
            "comment": d.comment,
        })
    return sorted(trades, key=lambda x: x["time"], reverse=True)

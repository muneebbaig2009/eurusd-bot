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
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result else "None"
        print(f"[MT5] order_send failed: retcode={code}  {mt5.last_error()}")
        return None

    print(
        f"[MT5] Order placed: ticket={result.order}  "
        f"{direction} {lot} lots  SL={sl:.5f}  TP={tp:.5f}"
    )
    return result.order  # position ticket (same as order ticket for market orders)


def get_position_result(ticket: int):
    """Return (status, close_price) if the position is closed, else None.

    Checks live positions first; if the ticket is gone, scans deal history for
    the closing deal (DEAL_ENTRY_OUT) to determine WIN vs LOSS.
    """
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
            status = "WIN" if deal.profit > 0 else "LOSS"
            return status, deal.price

    return None

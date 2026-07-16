"""Adaptive Strategy Optimization Engine.

Public surface
--------------
from adaptive.scheduler import record_signal_context, on_trade_closed, run_cycle
from adaptive.reporter  import print_report, export_json
from adaptive.param_store import get_store
from adaptive.guard     import get_guard
from adaptive.regime    import detect_from_signal

All other modules are internal implementation details.

The engine is strictly additive — every call is wrapped so that a bug
here can never interrupt the main trading bot.  Set ADAPTIVE_ENABLED=False
in config.py to disable all adaptive behaviour instantly.
"""
from adaptive.param_store import get_store, AdaptiveParamStore  # noqa: F401
from adaptive.scheduler   import (                               # noqa: F401
    get_scheduler,
    on_trade_closed,
    record_signal_context,
    run_cycle,
)
from adaptive.reporter    import print_report, export_json       # noqa: F401
from adaptive.guard       import get_guard                       # noqa: F401
from adaptive.regime      import detect, detect_from_signal      # noqa: F401
from adaptive.stats       import RollingStats                    # noqa: F401

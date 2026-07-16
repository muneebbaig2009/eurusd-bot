"""Online weight learning — delegates to adaptive.weight_learner.

This module preserves its original public API (update_weights) so that
main.py and any other callers require zero changes.  The implementation
now routes through the adaptive engine's weight_learner module, which
provides the same per-trade nudge logic plus batch rebalancing support.
"""
import config


def update_weights(db: str, contributors: dict, direction: str, won: bool) -> None:
    """Nudge technique weights after a trade closes.

    contributors : {technique: vote}  — captured when the signal fired
    direction    : "BUY" or "SELL"
    won          : True if TP hit first, False if SL hit

    Delegates to adaptive.weight_learner.update_per_trade which replicates
    the original logic and also feeds the batch-rebalancing pipeline.
    """
    try:
        from adaptive.weight_learner import update_per_trade
        update_per_trade(db, contributors, direction, won)
    except Exception:
        # Fallback: replicate original behaviour so learning never stops
        import storage
        dir_sign = 1 if direction == "BUY" else -1
        for tech, vote in contributors.items():
            if vote == 0:
                continue
            agreed = (vote == dir_sign)
            w = storage.get_weight(db, tech)
            if won and agreed:
                w += config.LEARN_RATE
            elif won and not agreed:
                w -= config.LEARN_RATE
            elif (not won) and agreed:
                w -= config.LEARN_RATE
            else:
                w += config.LEARN_RATE
            storage.set_weight(db, tech, w)

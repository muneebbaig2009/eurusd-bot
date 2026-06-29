"""Online weight learning. After a signal closes WIN/LOSS, nudge the weights of
the techniques that contributed in the signal's direction."""
import config
import storage


def update_weights(db, contributors: dict, direction: str, won: bool):
    """
    contributors: {technique: vote} captured when the signal fired.
    direction: BUY or SELL.
    won: True if TP hit first, False if SL hit.

    A technique 'agreed' with the trade if its vote sign matches the direction.
    Agreeing techniques get rewarded on a win and penalized on a loss.
    """
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
        elif (not won) and not agreed:
            w += config.LEARN_RATE
        storage.set_weight(db, tech, w)

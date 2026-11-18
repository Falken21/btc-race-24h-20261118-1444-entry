"""BTC race bot: selective long-only trend following with a cost filter.

Speaks agent-fun/bars-replay/v1 through the kit SDK in vendor/. The grader
runs ``python3 -P -s /bot/main.py``; -P leaves this script's directory off
sys.path, so vendor/ is added below before the SDK is imported.

Design notes (from grading on the dev window): the replay's cbBTC pool is
thin and quiet — per-bar volatility is only a few basis points, while a
full-size round trip costs about 26 bps in fees, slippage and price impact.
A bot that trades every wiggle pays the costs over and over and loses to
them, and a bot that chases short, weak moves in a falling or sideways
window loses to the reversals. So this bot is deliberately selective: it
trades only well-established uptrends, sizes them large, banks part of a
good move early, and sits out everything else. Cash is a position.

Strategy (all decisions are a pure function of the bars seen so far):

- Keep the 1-minute closes of the traded symbol, a fast EMA (30 bars), a
  slow EMA (120 bars) and a realised-volatility estimate (stdev of the
  last 30 log returns).
- Enter long, sized at 95% of equity, only when the fast EMA is above the
  slow EMA, the return over the last 120 bars exceeds +65 bps (a real
  trend, worth several round trips of cost), and volatility is not
  extreme. A cooldown of 30 bars follows every exit.
- While holding: bank half the position once its value over its cost
  reaches +1%; exit the rest when the fast EMA crosses back below the
  slow EMA or the close falls 70 bps below the peak close since entry
  (trailing stop).

Deterministic: no clock, no environment, no randomness at all.
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

# --- parameters (kept few on purpose: robust beats fitted) -------------------
FAST_N = 30            # fast EMA length, bars
SLOW_N = 120           # slow EMA length, bars
TREND_BARS = 120       # lookback for the trend-strength filter, bars
TREND_MIN = 0.0065     # min return over TREND_BARS to enter (65 bps)
VOL_N = 30             # log returns in the volatility estimate
VOL_MAX = 0.0035       # skip entries above this per-bar stdev (35 bps)
SIZE_FRAC = 0.95       # share of equity spent on an entry
TRAIL_OFF = 0.0070     # trailing stop: exit 70 bps below the peak close
PARTIAL_TP = 0.0100    # bank half once value/cost - 1 reaches +1%
COOLDOWN_BARS = 30     # bars to sit out after an exit decision
MAX_SLIPPAGE_BPS = 150 # generous: normal impact must never reject an order
MIN_HISTORY = 150      # closes required before the first entry

# --- mutable strategy state (a pure function of the bars seen) --------------
_symbol = None         # resolved from hello in on_hello
_closes = []           # every close of the symbol, in order
_ema_fast = None
_ema_slow = None
_peak_close = None     # highest close since the current position appeared
_entry_qty = None      # position size when the current position appeared
_cooldown = 0          # bars (with a bar for the symbol) left to sit out


def _resolve_symbol(state):
    global _symbol
    if "CBBTC" in state.symbols:
        _symbol = "CBBTC"
    else:
        _symbol = next(iter(state.symbols))


def _update_indicators(close):
    global _ema_fast, _ema_slow
    _closes.append(close)
    if _ema_fast is None:
        _ema_fast = float(close)
        _ema_slow = float(close)
        return
    kf = 2.0 / (FAST_N + 1)
    ks = 2.0 / (SLOW_N + 1)
    _ema_fast = close * kf + _ema_fast * (1.0 - kf)
    _ema_slow = close * ks + _ema_slow * (1.0 - ks)


def _trend_return():
    """Return over the last TREND_BARS closes, or None without the history."""
    if len(_closes) <= TREND_BARS:
        return None
    base = _closes[-1 - TREND_BARS]
    if base <= 0:
        return None
    return _closes[-1] / base - 1.0


def _volatility():
    """Stdev of the last VOL_N log returns, or None without the history."""
    if len(_closes) <= VOL_N:
        return None
    rets = []
    window = _closes[-1 - VOL_N:]
    for prev, cur in zip(window, window[1:]):
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def _entry_order(state):
    """A buy order if every entry condition holds on this bar, else None."""
    if state.warming_up or _cooldown > 0:
        return None
    if len(_closes) < MIN_HISTORY or _ema_fast is None:
        return None
    if _ema_fast <= _ema_slow:
        return None
    trend = _trend_return()
    if trend is None or trend <= TREND_MIN:
        return None
    vol = _volatility()
    if vol is None or vol > VOL_MAX:
        return None
    notional = int(SIZE_FRAC * state.equity)
    notional = min(notional, state.cash, state.weight_room(_symbol))
    min_notional = int(state.hello["fees"]["min_notional_quote_units"])
    if notional < max(min_notional, 1):
        return None
    return {
        "side": "buy",
        "symbol": _symbol,
        "notional_quote_units": notional,
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }


def _holding_orders(state, holding):
    """Orders for an open position: a one-off partial bank, or a full exit.

    The partial bank is recognised by the position having shrunk below
    90% of the size it had when it appeared, so if a sell is rejected
    (no bar at the next open) the bot simply offers it again next bar.
    """
    global _peak_close, _entry_qty, _cooldown
    close = _closes[-1]
    if _entry_qty is None:
        _entry_qty = holding.qty
        _peak_close = close
    elif close > _peak_close:
        _peak_close = close
    value = state.value(_symbol, holding.qty)
    gain = (value / holding.cost - 1.0) if holding.cost > 0 else 0.0
    if gain >= PARTIAL_TP and holding.qty * 10 >= _entry_qty * 9:
        return [{
            "side": "sell",
            "symbol": _symbol,
            "qty": holding.qty // 2,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]
    exit_now = (
        (_ema_fast is not None and _ema_slow is not None and _ema_fast < _ema_slow)
        or close <= _peak_close * (1.0 - TRAIL_OFF)
    )
    if not exit_now:
        return []
    _peak_close = None
    _entry_qty = None
    _cooldown = COOLDOWN_BARS
    return [{
        "side": "sell",
        "symbol": _symbol,
        "qty": holding.qty,
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }]


def on_batch(state, batch):
    global _cooldown, _peak_close, _entry_qty
    bar = None
    for b in batch["bars"]:
        if b["symbol"] == _symbol:
            bar = b
            break
    if bar is None:
        # No bar for the symbol this minute: nothing new to learn or decide.
        return []
    _update_indicators(int(bar["c"]))
    if _cooldown > 0:
        _cooldown -= 1
    holding = state.positions.get(_symbol)
    if holding is not None and holding.qty > 0:
        return _holding_orders(state, holding)
    _peak_close = None
    _entry_qty = None
    order = _entry_order(state)
    return [order] if order is not None else []


def on_hello(state):
    _resolve_symbol(state)


if __name__ == "__main__":
    makefun_bars.run(on_batch, on_hello=on_hello)

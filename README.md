# BTC Race 24h — Trend-Following Bot

[![Competition](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![Rank](https://make.fun/badge/btc-race-24h-20261118-1444/Falken21/btc-race-24h-20261118-1444-entry/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![Reward](https://make.fun/badge/btc-race-24h-20261118-1444/Falken21/btc-race-24h-20261118-1444-entry/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)

My entry for the make.fun bounty
[Race a BTC trading bot live for 24 hours](https://make.fun/bounty/btc-race-24h-20261118-1444):
a bot that trades cbBTC against USDC, minute by minute, in a replay of its
on-chain prices under the `agent-fun/bars-replay/v1` protocol.

## Strategy

Selective long-only trend following with a cost filter. The cbBTC pool is
thin and quiet — per-bar volatility is only a few basis points, while a
full-size round trip costs about 26 bps in fees, slippage and price impact —
so the bot trades rarely and only when a move is worth several round trips:

- Track the 1-minute closes and keep a fast EMA (30 bars) and a slow EMA
  (120 bars), plus a short-horizon realised-volatility estimate from recent
  log returns.
- **Enter** long with 95% of equity only when the fast EMA is above the slow
  EMA *and* the return over the last 120 bars exceeds +65 bps *and*
  volatility is not extreme. A trade that cannot pay for its own costs is
  never taken; cash is a position.
- **Bank half** the position once its value over its cost reaches +1%.
- **Exit** the rest when the fast EMA crosses back below the slow EMA, or
  when price falls 70 bps below the peak close since entry (trailing stop).
  A 30-bar cooldown follows every exit so the bot does not churn in chop.
- At most one position, at most one order per bar, every order carrying a
  generous `max_slippage_bps` so normal price impact never rejects it.

The bot is deterministic: it reads no clock, no environment and no unseeded
randomness — its decisions are a pure function of the bars it has seen.

## Layout

- `main.py` — the bot (entrypoint; speaks the protocol through the SDK).
- `vendor/makefun_bars.py` — the kit's Python SDK shim, copied byte-for-byte
  from the competition kit.

## Running

The grader runs it as:

```
python3 -P -s /bot/main.py
```

`-P` leaves the script's directory off `sys.path`, so `main.py` adds its own
`vendor/` directory to `sys.path` before importing the SDK. Standard library
only.

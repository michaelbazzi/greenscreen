# GreenScreen — autotrader

Autonomous, research-driven paper trading on top of the core buy-and-hold
strategy (`../deploy.py`) and manual executor (`../execute_trade.py`). This
subsystem lets a scheduled research agent decide *what* to trade, but it
never decides *whether* a trade is allowed — that's `propose_trade.py`'s job,
enforced from `risk_params.py`, recomputed from live market data every time.

**Paper trading only.** There is no `--live` flag anywhere in this
subpackage. Going live is a separate, deliberate, manual step outside of
anything autonomous — see `../execute_trade.py`'s confirmation-phrase gate,
which this system never touches.

## Files

- `risk_params.py` — every hard-coded guardrail. **This is the file to edit
  when tuning behavior.** Includes the master kill switch (`ENABLED`).
- `market_data.py` — pulls Alpaca historical bars (same paper keys) and
  computes a deterministic technical score (0-1) per ticker. This score is
  *never* accepted as an argument from the research step — it's always
  recomputed here, so a persuasive rationale alone can't satisfy the gate.
- `db.py` — shared, WAL-enabled SQLite connection helper. Adds a `decisions`
  table to `../trades.db` logging every proposal, approved or rejected.
- `propose_trade.py` — the only script allowed to place an autonomous order.

## Commands

```bash
# Check account state, today's usage against caps, circuit breaker status
python autotrader/propose_trade.py status

# Force-sell any position breaching its stop-loss. Always run this first,
# every cycle — it's exempt from the daily new-position cap.
python autotrader/propose_trade.py sweep-stop-loss --run-id <cycle-id>

# Propose a trade. No --conviction flag exists on purpose — the technical
# score is computed live from market data, not supplied by the caller.
python autotrader/propose_trade.py propose buy AAPL --notional 50 \
    --rationale "..." --run-id <cycle-id> [--source-url ...] [--sector technology]

python autotrader/propose_trade.py propose sell AAPL --notional 50 \
    --rationale "..." --run-id <cycle-id>

# Add --dry-run to any propose/sweep-stop-loss call to test guardrails
# without submitting a real (paper) order.

# Clear a tripped weekly circuit breaker (daily ones auto-clear next day)
python autotrader/propose_trade.py reset-circuit-breaker
```

`--run-id` should be the same value for every call made within one research
cycle (e.g. a timestamp like `2026-08-19T14`). This does two things: it lets
the run-lock detect a genuinely overlapping cycle (different run_id, lock
still fresh) versus normal sequential calls within one cycle (same run_id),
and it makes duplicate proposals for the same ticker+action within a cycle
get refused instead of silently double-executed.

## Guardrails at a glance

See `risk_params.py` for the authoritative values and comments. Summary:

- Position/trade sizing caps (per-ticker, per-trade, smaller for new tickers)
- Minimum 10% cash reserve
- Per-position stop-loss (-15% core, -20% new tickers, wider on purpose)
- Max 4 new buys/day and a daily total notional cap, both separate from
  stop-loss sells (which are always allowed, uncapped)
- Max 10 tickers held, sector exposure caps (tighter if untagged)
- New-ticker admission: minimum trading history and liquidity floor
- Technical-score threshold to buy (higher bar for brand-new tickers)
- Daily (-8%, auto-clears) and weekly (-15%, manual clear) portfolio
  drawdown circuit breakers that block new buys — never sells
- Master kill switch (`ENABLED = False` stops everything instantly)

## Known residual risk

The scheduled research agent has its own Bash/web-search access and could,
in principle, bypass `propose_trade.py` and call Alpaca directly — including
if manipulated by adversarial content encountered during web research
(prompt injection). This isn't sandboxed away at this layer; it's
prompt-governed. The blast radius is bounded by the account being
paper-only, and by the daily run summary sent to you, which is the
compensating control — read it, don't just let it run silently. If anything
in a summary looks like it didn't go through `propose_trade.py` (e.g. a
trade with no matching `decisions` row), treat that as a real incident, not
noise.

## Clearing a stuck run-lock

If a cycle crashes mid-run, `autotrader/state/run.lock` can be left behind.
It self-clears after 30 minutes (`RUN_LOCK_STALE_MINUTES`), or delete it
manually if you're sure nothing else is running:

```bash
rm autotrader/state/run.lock
```

# GreenScreen

An autonomous, research-driven paper-trading system. The interesting part isn't that it trades — it's how it's kept honest: an LLM does open-ended research and proposes trade ideas, but it never gets to act on its own judgment. Every proposal is independently re-checked against deterministic, hard-coded risk rules before anything executes. The model brings ideas; the code holds the leash.

**Paper trading only.** No live-money path exists in the autonomous system, at all — going live would be a separate, deliberate, manual project.

## Why it's built this way

Giving an LLM a brokerage API key and a prompt is easy. Making its mistakes bounded is the actual problem. Two failure modes matter here:

- **Bad judgment** — the model gets talked into (or hallucinates) a bad trade. Solved by never trusting its stated confidence: a "conviction score" is recomputed independently from live market data on every single proposal, using only measurable inputs (price trend vs. moving average, recent momentum). A persuasive rationale alone cannot clear that bar.
- **Bad behavior** — the model's tool access gets misused, whether by its own error or by adversarial content it encounters while researching the open web (prompt injection is a real, acknowledged risk here — see [`autotrader/README.md`](autotrader/README.md#known-residual-risk) for how it's bounded rather than hand-waved away). Solved by a hard chokepoint: the research agent's shell access is scoped (via `.claude/settings.json`) to exactly two scripts and two read-only research tools — it cannot edit files, cannot read credentials, cannot call the brokerage API directly, cannot run arbitrary commands.

Every proposal — approved or rejected — is logged with its reasoning, so the system's decisions are auditable after the fact, not just in the moment.

## Architecture

```
scheduled research agent (Claude Code + WebSearch)
        │  proposes: "buy $40 of XOM, because ..."
        ▼
autotrader/propose_trade.py   ← the only path to a real order
        │  recomputes technical score from live data (autotrader/market_data.py)
        │  checks position size, cash reserve, sector exposure, daily
        │    caps, drawdown circuit breakers (autotrader/risk_params.py)
        ▼
   approved → Alpaca API → logged to trades.db + decisions table
   rejected → logged with the specific reason, nothing executes
```

- [`deploy.py`](deploy.py) — the original, validated core strategy: equal-weighted (20% each) buy-and-hold across 5 large-cap equities (AAPL, MSFT, JPM, XOM, JNJ), backtested in QuantConnect over an 11-year window against momentum and mean-reversion alternatives (buy-and-hold won on every risk-adjusted metric: Sharpe 0.72 vs. -2.8 / -3.8).
- [`execute_trade.py`](execute_trade.py) — manual, human-run trade executor with its own safety rail: it refuses to touch a live account unless an operator types an exact confirmation phrase.
- [`autotrader/`](autotrader) — the autonomous layer described above. Runs unattended on a schedule (4x/day, market-hours-gated) via a local launchd job. Full breakdown of every guardrail, the run-lock/idempotency handling, and the circuit breakers is in [`autotrader/README.md`](autotrader/README.md).

## Current status

Live and running unattended. As of this writing: ~$1,121 portfolio value (from a $1,000 paper deposit), 5 core holdings, no path to add further paper capital (Alpaca doesn't support incremental deposits to a paper account — see [Alpaca's own docs](https://docs.alpaca.markets/docs/paper-trading)), so the system self-funds via sells only. Every research cycle's proposals — approved and rejected — are logged to `trades.db`'s `decisions` table for a full audit trail.

## Stack

Python, [alpaca-py](https://github.com/alpacahq/alpaca-py), Claude Code (as the scheduled research/reasoning agent), SQLite (WAL mode), launchd. Strategy backtesting in QuantConnect (LEAN engine).

## Running it yourself

```bash
pip install -r requirements-dev.txt
cp config.py.example config.py   # fill in your own Alpaca paper-trading keys
python -m pytest autotrader/ -v  # guardrail logic is fully unit tested, no API calls needed
python deploy.py                 # one-shot: establish the core buy-and-hold position
python autotrader/propose_trade.py status
```

The autonomous research loop itself (`autotrader/run_cycle.sh`) is driven by the Claude Code CLI and expects `CLAUDE_CODE_OAUTH_TOKEN` set — see [`autotrader/README.md`](autotrader/README.md) for the full setup, including the scheduled-job configuration.

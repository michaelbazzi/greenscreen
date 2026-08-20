# GreenScreen

Automated deployment script for a validated buy-and-hold equity strategy, executing trades via the Alpaca Trading API — plus an autonomous, research-driven trading engine (see `autotrader/`) that proposes and executes further trades within hard-coded risk guardrails.

## Strategy
Equal-weighted (20% each) buy-and-hold across 5 large-cap equities: AAPL, MSFT, JPM, XOM, JNJ.

## Validation
Backtested in QuantConnect (2015-2026, 11-year window) against two active alternatives — mean reversion and momentum. Buy-and-hold outperformed both on every risk-adjusted metric:

| Metric | Buy & Hold | Momentum | Mean Reversion |
|---|---|---|---|
| Sharpe Ratio | 0.717 | -2.836 | -3.751 |
| CAGR | 19.3% | — | — |
| Max Drawdown | 32.3% | — | — |

## Current Status
Deployed to Alpaca paper trading ($500 simulated account) for a 3-month live validation period before any real-capital decision.

## Stack
Python, Alpaca-py SDK, virtualenv. Backtesting/strategy development in QuantConnect (LEAN engine).

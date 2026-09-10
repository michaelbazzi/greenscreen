"""
Provider-agnostic research step: a bounded tool-calling loop, run once per
user per cycle, in a subprocess that already has GREENSCREEN_CONFIG_DIR set
for that user (see scheduler.py) - so every import below transparently
resolves to that user's own credentials, risk_params, and trades.db via
autotrader/config_loader.py, with zero per-tool plumbing needed here.

Exactly four tools are exposed to the model - screen_candidates,
get_technical_score, web_search, propose - because the harness simply
doesn't implement anything else. That's the whole safety boundary: not a
config flag that has to stay correctly set, but an actual absence of any
other capability. No file access, no arbitrary code execution, no direct
broker calls - "propose" is the only tool that can cause anything to
happen, and it goes through propose_trade.py's real, independent gate,
unchanged.

propose() is a subprocess call to propose_trade.py, not an in-process
import: cmd_propose() calls sys.exit(1) on a rejected proposal (correct
for a CLI script), which would kill this whole research loop if imported
directly. Subprocess isolation is what lets the model see "rejected,
here's why" and keep reasoning, exactly like the live Claude-Code-driven
research step already experiences today.
"""

import json
import subprocess
import sys
from pathlib import Path

import litellm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "autotrader"))

import market_data as md
import screen_candidates as sc

_AUTOTRADER_DIR = Path(__file__).resolve().parent.parent / "autotrader"
_PROPOSE_TRADE = _AUTOTRADER_DIR / "propose_trade.py"

MAX_TOOL_CALLS = 15

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "screen_candidates",
            "description": (
                "Real, structured market-mover data from Alpaca's own screener - "
                "today's most-active-by-volume stocks and top gainers/losers. A "
                "raw screen, not a recommendation - every name still has to clear "
                "the technical score and the full risk gate before anything executes."
            ),
            "parameters": {
                "type": "object",
                "properties": {"top": {"type": "integer", "description": "how many per category, default 15"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_technical_score",
            "description": (
                "The real, deterministic technical score (0-1) for a ticker, computed "
                "from live price/volume data - the same number propose_trade.py's gate "
                "will independently recompute and check. Not something you can influence "
                "or set directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web - for sentiment, news, insider activity, institutional positioning, or general catalysts on a candidate.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose",
            "description": (
                "Propose a trade. This does NOT execute anything by itself - it's "
                "independently re-checked by propose_trade.py's real gate (technical "
                "score recomputed from live data, every hard risk limit) before "
                "anything is submitted. A persuasive rationale alone cannot clear "
                "that bar. Rejections are normal and expected, not failures."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "notional": {"type": "number", "description": "dollar amount"},
                    "rationale": {"type": "string"},
                    "sector": {"type": "string", "description": "optional, for a ticker not already tagged"},
                },
                "required": ["ticker", "side", "notional", "rationale"],
            },
        },
    },
]


def _tool_screen_candidates(args: dict) -> dict:
    return sc.get_candidates(top=args.get("top", 15))


def _tool_get_technical_score(args: dict) -> dict:
    ticker = args["ticker"].upper()
    signals = md.compute_signals(ticker)
    if signals is None:
        return {"ticker": ticker, "error": "insufficient price history to compute a score"}
    score = md.technical_score(signals)
    return {"ticker": ticker, "score": score, "current_price": signals["current_price"]}


def _tool_web_search(args: dict, search_api_key: str | None) -> dict:
    if not search_api_key:
        return {"error": "no web search API key configured for this account"}
    import requests

    resp = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": search_api_key, "query": args["query"], "max_results": 5},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "results": [
            {"title": r.get("title"), "url": r.get("url"), "content": r.get("content")}
            for r in data.get("results", [])
        ]
    }


def _tool_propose(args: dict, run_id: str) -> dict:
    """Subprocess, not in-process - see module docstring for why."""
    cmd = [
        sys.executable,
        str(_PROPOSE_TRADE),
        "propose",
        args["side"],
        args["ticker"].upper(),
        "--notional",
        str(args["notional"]),
        "--rationale",
        args["rationale"],
        "--run-id",
        run_id,
    ]
    if args.get("sector"):
        cmd += ["--sector", args["sector"]]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    output = (result.stdout + result.stderr).strip()
    approved = output.startswith("APPROVED")
    return {"approved": approved, "output": output}


def _execute_tool(name: str, args: dict, run_id: str, search_api_key: str | None) -> dict:
    if name == "screen_candidates":
        return _tool_screen_candidates(args)
    if name == "get_technical_score":
        return _tool_get_technical_score(args)
    if name == "web_search":
        return _tool_web_search(args, search_api_key)
    if name == "propose":
        return _tool_propose(args, run_id)
    return {"error": f"unknown tool: {name}"}


def run_research_cycle(
    provider: str,
    llm_api_key: str,
    run_id: str,
    prompt: str,
    search_api_key: str | None = None,
) -> dict:
    """Runs the bounded tool-calling loop. Returns
    {"summary": str, "tool_calls": [...]} - a transcript-shaped record of
    every tool call made and its result, for the dashboard's decision log."""
    messages = [{"role": "user", "content": prompt}]
    tool_call_log = []

    for _ in range(MAX_TOOL_CALLS):
        response = litellm.completion(
            model=provider,
            api_key=llm_api_key,
            messages=messages,
            tools=TOOLS,
        )
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)

        if not tool_calls:
            return {"summary": message.content or "", "tool_calls": tool_call_log}

        messages.append(message.model_dump())
        for call in tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError:
                args = {}
            result = _execute_tool(name, args, run_id, search_api_key)
            tool_call_log.append({"tool": name, "args": args, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result),
                }
            )

    return {
        "summary": f"Hit the {MAX_TOOL_CALLS}-tool-call cap for this cycle without a final summary.",
        "tool_calls": tool_call_log,
    }


def _load_prompt() -> str:
    return (Path(__file__).resolve().parent / "research_prompt.md").read_text()


def main():
    """CLI entrypoint - run as a fresh subprocess per user cycle, never
    imported and called in-process by a long-running scheduler. Reason:
    config_loader resolves credentials once at module import time, so a
    shared process reusing this module across users would silently keep
    the first user's credentials for everyone after. See scheduler.py."""
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    provider = os.environ["GREENSCREEN_LLM_PROVIDER"]
    llm_api_key = os.environ["GREENSCREEN_LLM_API_KEY"]
    search_api_key = os.environ.get("GREENSCREEN_SEARCH_API_KEY")

    result = run_research_cycle(
        provider=provider,
        llm_api_key=llm_api_key,
        run_id=args.run_id,
        prompt=_load_prompt(),
        search_api_key=search_api_key,
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()

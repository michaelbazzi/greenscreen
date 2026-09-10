"""
GreenScreen web app: signup, onboarding, and a dashboard over each user's
own account. Deliberately does not import propose_trade/market_data's
module-level state directly in this long-running process - those modules
resolve credentials once at import time (see config_loader.py), so a
shared server process reusing them across requests would silently lock in
whichever user's config was imported first. Anything here that needs a
specific user's data either constructs its own TradingClient/sqlite
connection from that user's decrypted credentials per request (safe -
both are pure functions of what's passed in, not module globals), or
delegates to a subprocess (scheduler.py, for anything that runs a cycle).
"""

import sqlite3
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import user_store as us
from scheduler import start_background_scheduler

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "autotrader"))
from alpaca.trading.client import TradingClient
from propose_trade import get_snapshot

app = FastAPI(title="GreenScreen")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")

SESSION_COOKIE = "greenscreen_session"

# The exact fields onboarding's risk-settings form exposes, in display
# order - a subset of risk_params.py's constants; TICKER_SECTORS is
# intentionally left out of the form (a dict, not a simple field) and
# stays at its live default (empty per-user, tightening new-ticker
# exposure to the unknown-sector cap - see risk_params.py).
RISK_PARAM_FIELDS = [
    "MAX_POSITION_PCT",
    "MAX_TRADE_PCT",
    "MAX_TRADE_PCT_NEW_TICKER",
    "MIN_CASH_RESERVE_PCT",
    "STOP_LOSS_PCT",
    "STOP_LOSS_PCT_NEW_TICKER",
    "MAX_NEW_POSITIONS_PER_DAY",
    "MAX_DAILY_NOTIONAL_DEPLOYED_PCT",
    "MAX_TICKERS_HELD",
    "MAX_SECTOR_EXPOSURE_PCT",
    "MAX_SECTOR_EXPOSURE_PCT_UNKNOWN",
    "NEW_TICKER_MIN_TRADING_DAYS",
    "NEW_TICKER_MIN_AVG_DOLLAR_VOLUME",
    "TECH_SCORE_THRESHOLD_EXISTING",
    "TECH_SCORE_THRESHOLD_NEW",
    "DAILY_DRAWDOWN_CIRCUIT_BREAKER_PCT",
    "WEEKLY_DRAWDOWN_CIRCUIT_BREAKER_PCT",
    "ROTATION_MIN_SCORE_EDGE",
]


@app.on_event("startup")
def _startup():
    app.state.scheduler = start_background_scheduler()


def get_current_user(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return auth.verify_session_token(token)


def require_user(request: Request) -> str:
    user_id = get_current_user(request)
    if user_id is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user_id


# --- landing / auth -----------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if get_current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return templates.TemplateResponse(request, "signup.html", {"error": None})


@app.post("/signup")
def signup_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        user_id = auth.create_account(email, password)
    except auth.EmailAlreadyRegistered:
        return templates.TemplateResponse(
            request, "signup.html", {"error": "That email's already registered."}
        )
    response = RedirectResponse("/onboarding/connect-alpaca", status_code=303)
    response.set_cookie(SESSION_COOKIE, auth.create_session_token(user_id), httponly=True, samesite="lax")
    return response


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        user_id = auth.verify_login(email, password)
    except auth.InvalidLogin:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Wrong email or password."}
        )
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(SESSION_COOKIE, auth.create_session_token(user_id), httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- onboarding -----------------------------------------------------------

@app.get("/onboarding/connect-alpaca", response_class=HTMLResponse)
def connect_alpaca_form(request: Request, user_id: str = Depends(require_user)):
    return templates.TemplateResponse(request, "onboarding_alpaca.html", {"error": None})


@app.post("/onboarding/connect-alpaca")
def connect_alpaca_submit(
    request: Request,
    user_id: str = Depends(require_user),
    api_key: str = Form(...),
    api_secret: str = Form(...),
):
    try:
        client = TradingClient(api_key, api_secret, paper=True)
        client.get_account()
    except Exception:
        return templates.TemplateResponse(
            request,
            "onboarding_alpaca.html",
            {"error": "Couldn't connect with those keys - double-check them and try again."},
        )
    # llm fields filled in on the next step; placeholders until then
    us.save_credentials(user_id, api_key, api_secret, llm_provider="", llm_api_key="")
    return RedirectResponse("/onboarding/connect-llm", status_code=303)


@app.get("/onboarding/connect-llm", response_class=HTMLResponse)
def connect_llm_form(request: Request, user_id: str = Depends(require_user)):
    return templates.TemplateResponse(request, "onboarding_llm.html", {"error": None})


@app.post("/onboarding/connect-llm")
def connect_llm_submit(
    request: Request,
    user_id: str = Depends(require_user),
    llm_provider: str = Form(...),
    llm_api_key: str = Form(...),
):
    existing = us.load_credentials(user_id)
    us.save_credentials(user_id, existing["api_key"], existing["api_secret"], llm_provider, llm_api_key)
    return RedirectResponse("/onboarding/risk-settings", status_code=303)


@app.get("/onboarding/risk-settings", response_class=HTMLResponse)
def risk_settings_form(request: Request, user_id: str = Depends(require_user)):
    defaults = us.live_risk_param_defaults()
    fields = [(name, defaults[name]) for name in RISK_PARAM_FIELDS]
    return templates.TemplateResponse(request, "onboarding_risk.html", {"fields": fields})


@app.post("/onboarding/risk-settings")
async def risk_settings_submit(request: Request, user_id: str = Depends(require_user)):
    form = await request.form()
    defaults = us.live_risk_param_defaults()
    params = dict(defaults)  # ENABLED, ROTATION_ENABLED, TICKER_SECTORS etc. stay at live defaults
    for name in RISK_PARAM_FIELDS:
        params[name] = float(form[name])
    us.save_risk_params(user_id, params)
    return RedirectResponse("/onboarding/disclaimer", status_code=303)


@app.get("/onboarding/disclaimer", response_class=HTMLResponse)
def disclaimer_form(request: Request, user_id: str = Depends(require_user)):
    return templates.TemplateResponse(request, "onboarding_disclaimer.html", {})


@app.post("/onboarding/disclaimer")
def disclaimer_submit(user_id: str = Depends(require_user), acknowledged: str = Form(...)):
    if acknowledged != "yes":
        raise HTTPException(status_code=400, detail="Acknowledgment is required to proceed.")
    auth.acknowledge_disclaimer(user_id)
    return RedirectResponse("/dashboard", status_code=303)


# --- dashboard -----------------------------------------------------------

def _onboarding_incomplete_redirect(user_id: str) -> RedirectResponse | None:
    user_dir = us.user_dir(user_id)
    if not (user_dir / "credentials.enc").exists():
        return RedirectResponse("/onboarding/connect-alpaca", status_code=303)
    creds = us.load_credentials(user_id)
    if not creds.get("llm_provider"):
        return RedirectResponse("/onboarding/connect-llm", status_code=303)
    if not (user_dir / "risk_params.json").exists():
        return RedirectResponse("/onboarding/risk-settings", status_code=303)
    if not auth.has_acknowledged_disclaimer(user_id):
        return RedirectResponse("/onboarding/disclaimer", status_code=303)
    return None


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user_id: str = Depends(require_user)):
    redirect = _onboarding_incomplete_redirect(user_id)
    if redirect:
        return redirect

    creds = us.load_credentials(user_id)
    client = TradingClient(creds["api_key"], creds["api_secret"], paper=True)
    snapshot = get_snapshot(client)

    db_path = us.user_dir(user_id) / "trades.db"
    decisions = []
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        try:
            decisions = conn.execute(
                "SELECT timestamp_utc, ticker, action, technical_score, risk_checks_passed, "
                "rejection_reason FROM decisions ORDER BY id DESC LIMIT 25"
            ).fetchall()
        except sqlite3.OperationalError:
            pass  # no decisions table yet - brand new account, nothing run
        conn.close()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"snapshot": snapshot, "decisions": decisions},
    )

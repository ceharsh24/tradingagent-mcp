"""MCP server that gives Claude TradingAgents' market data, memory and reports.

Claude itself plays the agents (analysts, bull/bear researchers, trader, risk
team, portfolio manager), following the trading-analysis skill, so the analysis
runs on the Claude plan of whoever is using Claude Code. TradingAgents supplies:

* point-in-time data: every dated tool takes the analysis date `as_of` and never
  returns data published after it, so a past date gives an honest backtest view;
* the memory log: past decisions, their realised returns and lessons;
* the report writer: the same report tree and HTML page the CLI saves.

Settings (all optional) go in ~/.tradingagents/.env; see .env.example. If an
API key for TradingAgents' configured provider is set there, the
original LangGraph pipeline is also exposed (run_pipeline and friends).

Run:  uv run server.py            (stdio; the plugin's .mcp.json does this)
      uv run server.py --http     (streamable HTTP on 127.0.0.1:8765)
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
# Settings live outside the plugin folder, which is replaced on every update.
# First match wins per variable; the real environment beats every file.
CONFIG_FILES = [
    Path(p).expanduser() for p in (
        os.getenv("TRADINGAGENTS_CLAUDE_ENV", ""),
        "~/.tradingagents/.env",
        str(HERE / ".env"),  # a development checkout
    ) if p
]
for _f in CONFIG_FILES:
    load_dotenv(_f, override=False)

from mcp.server.mcpserver import MCPServer  # noqa: E402

mcp = MCPServer(
    "tradingagents",
    instructions=(
        "Market data, memory and report tools from TradingAgents. Use the "
        "trading-analysis skill to run a full multi-agent analysis. Pass the same "
        "as_of date to every dated tool in one analysis. Research only: never "
        "present output as financial advice; these tools cannot place trades."
    ),
)

MAX_CHARS = 40_000
TICKER_RE = re.compile(r"^[A-Z0-9.\-=^]{1,20}$")


# ---------------------------------------------------------------- helpers

def _quiet(fn):
    """Keep library prints off stdout (the MCP stream in stdio mode) and turn
    vendor failures into text Claude can read instead of a tool crash."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with contextlib.redirect_stdout(sys.stderr):
            try:
                out = fn(*args, **kwargs)
            except ValueError:
                raise  # bad input: report as a tool error
            except Exception as exc:  # noqa: BLE001
                return f"[{fn.__name__} unavailable: {type(exc).__name__}: {exc}]"
        if isinstance(out, str) and len(out) > MAX_CHARS:
            out = out[:MAX_CHARS] + f"\n[truncated at {MAX_CHARS} characters]"
        return out

    return wrapper


def _ticker(raw: str) -> str:
    from tradingagents.dataflows.symbols import normalize_symbol

    t = normalize_symbol(raw.strip())
    if not TICKER_RE.match(t):
        raise ValueError(f"Not a ticker symbol: {raw!r}")
    return t


def _as_of(as_of: str | None) -> str:
    d = as_of or date.today().isoformat()
    if date.fromisoformat(d) > date.today():
        raise ValueError(f"as_of {d} is in the future")
    return d


def _days_before(d: str, n: int) -> str:
    return (date.fromisoformat(d) - timedelta(days=n)).isoformat()


def _route(method: str, *args):
    from tradingagents.dataflows.router import route_to_vendor

    return route_to_vendor(method, *args)


def _config() -> dict:
    from tradingagents.default_config import build_default_config

    return build_default_config()


def _memory():
    from tradingagents.memory.log import TradingMemoryLog

    return TradingMemoryLog(_config())


# ---------------------------------------------------------------- instrument

@mcp.tool()
@_quiet
def instrument_profile(ticker: str, as_of: str | None = None) -> str:
    """Identify the instrument: company name and classification, the exact ticker to
    use, and whether it is a stock or crypto. Call this first, and give the result to
    every agent so none of them confuse the company with another.

    Args:
        ticker: Yahoo Finance symbol with exchange suffix, e.g. NVDA, RELIANCE.NS,
            0700.HK, AZN.L, BTC-USD.
        as_of: Analysis date YYYY-MM-DD (default today).
    """
    from tradingagents.agents.context import build_instrument_context, resolve_instrument_identity
    from tradingagents.dataflows.symbols import crypto_base

    t, d = _ticker(ticker), _as_of(as_of)
    asset_type = "crypto" if crypto_base(t) else "stock"
    context = build_instrument_context(t, asset_type, resolve_instrument_identity(t), d)
    return f"Ticker: {t}\nAsset type: {asset_type}\nAnalysis date: {d}\n\n{context}"


# ---------------------------------------------------------------- market

@mcp.tool()
@_quiet
def market_snapshot(ticker: str, as_of: str | None = None, look_back_days: int = 30) -> str:
    """Verified market snapshot: the latest OHLCV row on or before as_of, common
    indicators (moving averages, RSI, MACD, Bollinger, ATR) and recent closes. Treat it
    as the source of truth for any exact price or indicator figure you state."""
    from tradingagents.dataflows.vendors.yahoo.snapshot import build_verified_market_snapshot

    return build_verified_market_snapshot(_ticker(ticker), _as_of(as_of), look_back_days)


@mcp.tool()
@_quiet
def price_history(ticker: str, start_date: str, as_of: str | None = None) -> str:
    """Daily OHLCV prices from start_date (YYYY-MM-DD) up to as_of."""
    from tradingagents.dataflows.date_window import as_of_window

    d = _as_of(as_of)
    start, end = as_of_window(start_date, d, d)
    return _route("get_stock_data", _ticker(ticker), start, end)


@mcp.tool()
@_quiet
def technical_indicators(
    ticker: str,
    indicators: list[str],
    as_of: str | None = None,
    look_back_days: int = 30,
) -> str:
    """Daily values of technical indicators over the look-back window ending at as_of.

    Choose up to 8 that complement each other (avoid redundant pairs):
    close_50_sma, close_200_sma (trend); close_10_ema (short-term momentum);
    macd, macds, macdh (momentum, crossovers, divergence); rsi (overbought/oversold,
    70/30); boll, boll_ub, boll_lb (volatility bands, breakouts); atr (volatility,
    stop distances); vwma (volume-confirmed trend); mfi (money flow).
    """
    t, d = _ticker(ticker), _as_of(as_of)
    out = []
    for ind in dict.fromkeys(i.strip().lower() for i in indicators if i.strip()):
        try:
            out.append(_route("get_indicators", t, ind, d, look_back_days))
        except ValueError as exc:
            out.append(f"{ind}: {exc}")
    return "\n\n".join(out)


# ---------------------------------------------------------------- fundamentals

@mcp.tool()
@_quiet
def fundamentals(ticker: str, as_of: str | None = None) -> str:
    """Company fundamentals as of the date: valuation, profitability, growth, balance
    sheet strength. US filers come from SEC EDGAR as originally filed."""
    return _route("get_fundamentals", _ticker(ticker), _as_of(as_of))


@mcp.tool()
@_quiet
def financial_statement(
    ticker: str,
    statement: Literal["income_statement", "balance_sheet", "cashflow"],
    freq: Literal["quarterly", "annual"] = "quarterly",
    as_of: str | None = None,
) -> str:
    """One financial statement as it stood on as_of (no later restatements or filings)."""
    return _route(f"get_{statement}", _ticker(ticker), freq, _as_of(as_of))


@mcp.tool()
@_quiet
def insider_transactions(ticker: str, as_of: str | None = None) -> str:
    """Insider buys and sells. Withheld for past dates, since they are dated by trade
    rather than by filing."""
    return _route("get_insider_transactions", _ticker(ticker), _as_of(as_of))


# ---------------------------------------------------------------- news and sentiment

@mcp.tool()
@_quiet
def company_news(ticker: str, as_of: str | None = None, days: int = 7) -> str:
    """News about the company from the `days` before as_of."""
    d = _as_of(as_of)
    return _route("get_news", _ticker(ticker), _days_before(d, days), d)


@mcp.tool()
@_quiet
def global_news(as_of: str | None = None, days: int = 7, limit: int = 20) -> str:
    """Global macro and market news from the `days` before as_of."""
    return _route("get_global_news", _as_of(as_of), days, limit)


@mcp.tool()
@_quiet
def social_sentiment(ticker: str, as_of: str | None = None, days: int = 7) -> str:
    """Recent StockTwits messages and Reddit posts about the ticker from the `days`
    before as_of. Posts are screened for relevance when TYPESAFE_API_KEY is set."""
    from tradingagents.agents.post_screen import jev_screen
    from tradingagents.dataflows.vendors.reddit import fetch_reddit_posts, subreddits_for
    from tradingagents.dataflows.vendors.stocktwits import fetch_stocktwits_messages

    t, d = _ticker(ticker), _as_of(as_of)
    start = _days_before(d, days)
    screen = jev_screen(t)
    subs = subreddits_for(t)
    st = fetch_stocktwits_messages(t, limit=30, start_date=start, end_date=d, screen=screen)
    rd = fetch_reddit_posts(t, subs, start_date=start, end_date=d, screen=screen)
    return f"## StockTwits\n{st}\n\n## Reddit (r/{', r/'.join(subs)})\n{rd}"


# ---------------------------------------------------------------- macro

@mcp.tool()
@_quiet
def macro_indicator(indicator: str, as_of: str | None = None, look_back_days: int = 365) -> str:
    """A macro series from FRED up to as_of. Aliases: cpi, core_pce, unemployment,
    fed_funds_rate, 10y_treasury, yield_curve, real_gdp, vix, ecb_deposit_rate,
    euro_hicp, eur_usd, or any raw FRED series ID. Needs FRED_API_KEY (free)."""
    return _route("get_macro_indicators", indicator, _as_of(as_of), look_back_days)


@mcp.tool()
@_quiet
def prediction_markets(topic: str, as_of: str | None = None, limit: int = 6) -> str:
    """Polymarket implied probabilities for events matching the topic, e.g.
    'Fed rate cut', 'recession 2026', or a company event."""
    return _route("get_prediction_markets", topic, limit, _as_of(as_of))


# ---------------------------------------------------------------- options

def _options_live(ticker: str, as_of: str | None) -> tuple[str, list[str]] | str:
    """(ticker, expiries), or the reason there is nothing to show."""
    import options as opt

    t = _ticker(ticker)
    if as_of and _as_of(as_of) != date.today().isoformat():
        return (f"Options data is withheld for {as_of}: Yahoo serves only today's chains, "
                "so there is no point-in-time history. Analyse options for today only.")
    expiries = opt.expirations(t)
    if not expiries:
        return (f"No listed options for {t} on Yahoo Finance. Yahoo carries mostly US listings "
                "(no NSE/BSE options). Proceed without an options view.")
    return t, expiries


def _pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def _num(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None or x != x else f"{x:,.{digits}f}"


@mcp.tool()
@_quiet
def options_overview(ticker: str, as_of: str | None = None) -> str:
    """Options market overview, computed in code from Yahoo's live chains: implied
    volatility term structure, market-implied move per expiry (from the at-the-money
    straddle), 25-delta skew, put/call open interest, max pain, recent realized
    volatility and the next earnings date. Today only; mostly US listings."""
    import options as opt

    live = _options_live(ticker, as_of)
    if isinstance(live, str):
        return live
    t, expiries = live
    rows, spot, summaries = [], None, []
    for exp in opt.pick_expiries(expiries):
        calls, puts, spot = opt.fetch_chain(t, exp)
        summaries.append(opt.summarize_expiry(calls, puts, spot, exp))
    hv = opt.realized_vols(t)
    earnings = opt.next_earnings(t)
    for x in summaries:
        rr = "n/a" if x.rr25 is None else f"{x.rr25:+.1f}"
        earn = " (earnings inside)" if earnings and earnings <= x.expiry else ""
        rows.append(f"| {x.expiry}{earn} | {x.dte} | {_pct(x.atm_iv)} | ±{_num(x.straddle)} "
                    f"(±{_pct(x.implied_move)}) | {rr} | {_num(x.pc_oi)} | {_num(x.max_pain)} |")
    month = min(summaries, key=lambda x: abs(x.dte - 30))
    ratio = (month.atm_iv / hv[20]) if month.atm_iv and hv.get(20) else None
    return "\n".join([
        f"## Options overview: {t} (live, {date.today().isoformat()})",
        f"Spot {_num(spot)} · {len(expiries)} expiries listed · next earnings: {earnings or 'not found'}",
        f"Realized volatility: 20-day {_pct(hv.get(20))} · 60-day {_pct(hv.get(60))}",
        f"~30-day ATM IV / 20-day realized: {_num(ratio)}x (above 1: options price more movement than recent history)",
        "",
        "| Expiry | DTE | ATM IV | Implied move (straddle) | 25Δ skew put−call (vol pts) | P/C open interest | Max pain |",
        "|---|---:|---:|---:|---:|---:|---:|",
        *rows,
        "",
        "Computed from Yahoo's live chain: mid prices where quoted, else last trade; IV as "
        "Yahoo reports it. Implied move ≈ the market's expected absolute move by expiry.",
    ])


@mcp.tool()
@_quiet
def options_chain(ticker: str, expiry: str | None = None, strikes_each_side: int = 6,
                  as_of: str | None = None) -> str:
    """One expiry in detail: the near-the-money strikes (bid/ask, IV, Black-Scholes
    delta, open interest, volume for calls and puts), implied move, 25-delta skew,
    put/call ratios, the biggest open-interest strikes and max pain. Default expiry:
    the first at least 21 days out. Today only."""
    import options as opt

    live = _options_live(ticker, as_of)
    if isinstance(live, str):
        return live
    t, expiries = live
    exp = expiry or opt.default_expiry(expiries)
    if exp not in expiries:
        raise ValueError(f"{t} has no {exp} expiry. Listed: {', '.join(expiries[:24])}")
    calls, puts, spot = opt.fetch_chain(t, exp)
    x = opt.summarize_expiry(calls, puts, spot, exp)
    T, r = opt.years_to(exp), opt.risk_free_rate()
    calls, puts = opt.with_deltas(calls, "call", spot, T, r), opt.with_deltas(puts, "put", spot, T, r)
    strikes = sorted(set(calls["strike"]) | set(puts["strike"]))
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
    n = max(1, min(strikes_each_side, 20))
    by_c, by_p = calls.set_index("strike"), puts.set_index("strike")

    def side(df, k):
        if k not in df.index:
            return "| | | | |"
        r_ = df.loc[k]
        quote = f"{_num(r_['bid'])}/{_num(r_['ask'])}" + ("" if r_["priced_from"] == "mid" else " (last)")
        return f"| {quote} | {_pct(r_['iv'] if r_['iv'] == r_['iv'] else None)} | {_num(r_['delta'])} | {int(r_['openInterest']):,}/{int(r_['volume']):,} "

    rows = [f"| **{_num(k)}** " + side(by_c, k) + side(by_p, k) + "|" for k in strikes[max(0, i - n): i + n + 1]]
    earnings = opt.next_earnings(t)
    walls = lambda w: ", ".join(f"{_num(k)} ({oi:,})" for k, oi in w) or "n/a"  # noqa: E731
    return "\n".join([
        f"## {t} options, {exp} ({x.dte} days) · spot {_num(spot)} · live {date.today().isoformat()}",
        f"ATM strike {_num(x.atm_strike)} · ATM IV {_pct(x.atm_iv)} · straddle {_num(x.straddle)} → "
        f"implied move ±{_pct(x.implied_move)} (range ≈ {_num(spot - (x.straddle or 0))}–{_num(spot + (x.straddle or 0))})",
        f"25Δ skew (put IV − call IV): {'n/a' if x.rr25 is None else f'{x.rr25:+.1f} vol pts'} · "
        f"P/C volume {_num(x.pc_volume)} · P/C open interest {_num(x.pc_oi)} · max pain {_num(x.max_pain)}",
        f"Largest call OI: {walls(x.call_walls)} · largest put OI: {walls(x.put_walls)}",
        f"Earnings before this expiry: {'yes, ' + earnings if earnings and earnings <= exp else 'no' if earnings else 'unknown'}",
        "",
        "| Strike | Call bid/ask | Call IV | Call Δ | Call OI/vol | Put bid/ask | Put IV | Put Δ | Put OI/vol |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        *rows,
        "",
        f"Deltas: Black-Scholes with each strike's IV and a {opt.risk_free_rate():.1%} rate (TA_RISK_FREE_RATE). "
        "One contract = 100 shares.",
    ])


@mcp.tool()
@_quiet
def options_strategy(ticker: str, legs: list[dict], as_of: str | None = None) -> dict | str:
    """Price an options position from the live chain and compute its payoff at expiry
    in code: net debit/credit, max profit, max loss, breakevens, probability of profit
    and net delta. Use it for every structure you propose; never do this math yourself.

    legs: list of {"type": "call"|"put"|"stock", "side": "buy"|"sell",
    "strike": number (options), "expiry": "YYYY-MM-DD" (options), "quantity": contracts
    for options or shares for stock (default 1), "price": optional per-share override}.
    All option legs must share one expiry. Examples: covered call = buy 100 stock +
    sell 1 call; bull call spread = buy call K1 + sell call K2; cash-secured put = sell 1 put.
    Prices default to the mid quote (else last trade).
    """
    import options as opt

    live = _options_live(ticker, as_of)
    if isinstance(live, str):
        return live
    t, expiries = live
    if not legs:
        raise ValueError("legs is required")
    opt_legs = [l for l in legs if l.get("type") in ("call", "put")]
    exps = {l.get("expiry") for l in opt_legs}
    if len(exps) > 1:
        raise ValueError("All option legs must share one expiry (calendar spreads are not supported).")
    exp = exps.pop() if exps else None
    if exp and exp not in expiries:
        raise ValueError(f"{t} has no {exp} expiry. Listed: {', '.join(expiries[:24])}")
    calls, puts, spot = opt.fetch_chain(t, exp or opt.default_expiry(expiries))

    priced, legs_out = [], []
    for raw in legs:
        kind, side_ = raw.get("type"), raw.get("side")
        if kind not in ("call", "put", "stock") or side_ not in ("buy", "sell"):
            raise ValueError(f"Bad leg {raw}: type must be call/put/stock and side buy/sell")
        qty = float(raw.get("quantity", 1))
        if qty <= 0:
            raise ValueError(f"Bad leg {raw}: quantity must be positive")
        if kind == "stock":
            price, iv, src = float(raw.get("price") or spot), None, "spot"
            strike = None
        else:
            strike = float(raw["strike"])
            df = calls if kind == "call" else puts
            match = df[df["strike"] == strike]
            if match.empty:
                near = df.iloc[(df["strike"] - strike).abs().argsort()[:4]]["strike"].tolist()
                raise ValueError(f"No {exp} {kind} at {strike}. Nearest strikes: {near}")
            row = match.iloc[0]
            iv = row["iv"] if row["iv"] == row["iv"] else None
            if raw.get("price") is not None:
                price, src = float(raw["price"]), "given"
            elif row["price"] == row["price"]:
                price, src = float(row["price"]), row["priced_from"]
            else:
                raise ValueError(f"No quote for the {exp} {kind} at {strike}; pass a price.")
        priced.append(opt.Leg(kind, side_, qty, strike, price, iv))
        legs_out.append({"type": kind, "side": side_, "quantity": qty, "strike": strike,
                         "price": round(price, 2), "priced_from": src,
                         "iv": None if iv is None else round(iv, 3)})

    sigma = opt.summarize_expiry(calls, puts, spot, exp).atm_iv if exp else None
    result = opt.analyze_strategy(priced, spot, exp, sigma)
    bounded = result["max_profit"] is not None and result["max_loss"] is not None and result["max_loss"] < 0
    return {
        "ticker": t, "spot": round(spot, 2), "expiry": exp,
        "dte": (date.fromisoformat(exp) - date.today()).days if exp else None,
        "legs": legs_out, **result,
        "return_on_risk": round(result["max_profit"] / -result["max_loss"], 2) if bounded else None,
        "pop_volatility": None if sigma is None else round(sigma, 3),
        "note": "At expiry, per position, before commissions and assignment risk. Probability "
                "of profit is risk-neutral under a lognormal at the ATM IV: a market-implied "
                "figure, not a forecast.",
    }


# ---------------------------------------------------------------- memory

@mcp.tool()
@_quiet
def memory_lessons(ticker: str, as_of: str | None = None) -> str:
    """Lessons from past decisions to give the Portfolio Manager: recent decisions on
    this ticker plus lessons from other tickers, limited to outcomes known by as_of."""
    ctx = _memory().get_past_context(_ticker(ticker), as_of=_as_of(as_of))
    return ctx or "No settled past decisions yet."


@mcp.tool()
@_quiet
def past_decisions(ticker: str | None = None, max_chars: int = 20_000) -> str:
    """The raw memory log: past decisions and, once settled, their realised return,
    alpha and reflection. Optionally filtered to one ticker."""
    path = Path(_config()["memory_log_path"])
    if not path.exists():
        return "No decisions logged yet."
    sep = "<!-- ENTRY_END -->"
    entries = [e.strip() for e in path.read_text(encoding="utf-8").split(sep) if e.strip()]
    if ticker:
        t = _ticker(ticker)
        # Each entry opens with a tag line: [YYYY-MM-DD | TICKER | rating | status]
        entries = [e for e in entries if f"| {t} |" in e.splitlines()[0].upper()]
        if not entries:
            return f"No logged decisions for {t}."
    return "\n\n---\n\n".join(entries)[-max_chars:]


@mcp.tool()
@_quiet
def unsettled_decisions() -> list[dict] | str:
    """Logged decisions whose holding window has now traded, with their realised raw
    return and alpha against the regional benchmark. For each, write a short
    reflection (what the call got right or wrong, and the lesson) and record it with
    settle_decision. Decisions still inside their window are listed as waiting."""
    from tradingagents.memory.settlement import fetch_returns, resolve_benchmark

    cfg, log = _config(), _memory()
    out = []
    for e in log.get_pending_entries():
        bench = resolve_benchmark(e["ticker"], cfg)
        item = {"ticker": e["ticker"], "trade_date": e["date"], "rating": e.get("rating"),
                "benchmark": bench}
        try:
            raw, alpha, days, resolved = fetch_returns(
                e["ticker"], e["date"], cfg.get("holding_period_days", 5), benchmark=bench)
        except Exception as exc:  # noqa: BLE001
            item["status"] = f"prices unavailable: {exc}"
        else:
            if raw is None:
                item["status"] = "waiting: holding window has not traded yet"
            else:
                item.update(status="ready", raw_return=round(raw, 4), alpha=round(alpha, 4),
                            holding_days=days, resolved=resolved,
                            decision=e.get("decision", "")[:3000])
        out.append(item)
    return out or "No unsettled decisions."


@mcp.tool()
@_quiet
def settle_decision(ticker: str, trade_date: str, reflection: str) -> str:
    """Record a decision's outcome and your reflection in the memory log. Returns are
    recomputed here from prices, so only the reflection comes from you. Keep it to a
    few sentences: was the call right, why, and the lesson for next time."""
    from tradingagents.memory.settlement import fetch_returns, resolve_benchmark

    t, cfg = _ticker(ticker), _config()
    date.fromisoformat(trade_date)
    bench = resolve_benchmark(t, cfg)
    raw, alpha, days, resolved = fetch_returns(
        t, trade_date, cfg.get("holding_period_days", 5), benchmark=bench)
    if raw is None:
        return f"{t} {trade_date}: the holding window has not traded yet; nothing recorded."
    ok = _memory().update_with_outcome(t, trade_date, raw, alpha, days, reflection.strip(),
                                       resolution_date=resolved)
    if not ok:
        return f"{t} {trade_date}: no pending decision found (already settled?)."
    return f"Settled {t} {trade_date}: return {raw:+.2%}, alpha {alpha:+.2%} vs {bench} over {days} days."


# ---------------------------------------------------------------- reports

@mcp.tool()
@_quiet
def save_analysis(
    ticker: str,
    trade_date: str,
    rating: Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"],
    final_decision: str,
    market_report: str = "",
    sentiment_report: str = "",
    news_report: str = "",
    fundamentals_report: str = "",
    bull_case: str = "",
    bear_case: str = "",
    research_plan: str = "",
    trader_plan: str = "",
    aggressive_view: str = "",
    conservative_view: str = "",
    neutral_view: str = "",
    options_analysis: str = "",
    options_strategy: str = "",
    fact_check: str = "",
    log_decision: bool = True,
) -> dict:
    """Save a finished analysis as TradingAgents' report tree (markdown sections plus
    one complete_report.html page) and log the decision to the memory log so it is
    scored once its holding window has traded. Pass each agent's full text in markdown.
    final_decision is the Portfolio Manager's write-up, starting with the rating line.
    options_analysis, options_strategy and fact_check add the Options and Fact Check
    sections when given.
    """
    from tradingagents.dataflows.symbols import safe_ticker_component
    from tradingagents.reporting import write_report_tree

    t, cfg = _ticker(ticker), _config()
    date.fromisoformat(trade_date)
    state = {
        "company_of_interest": t, "trade_date": trade_date,
        "market_report": market_report, "sentiment_report": sentiment_report,
        "news_report": news_report, "fundamentals_report": fundamentals_report,
        "investment_debate_state": {"bull_history": bull_case, "bear_history": bear_case},
        "investment_plan": research_plan, "trader_investment_plan": trader_plan,
        "risk_debate_state": {"aggressive_history": aggressive_view,
                              "conservative_history": conservative_view,
                              "neutral_history": neutral_view},
        "final_trade_decision": final_decision, "final_rating": rating,
        "memory_note": "decision logged" if log_decision else "not logged",
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = Path(cfg["results_dir"]) / "reports" / f"{safe_ticker_component(t)}_{stamp}"
    import tradingagents

    reports = {"market": market_report, "social": sentiment_report,
               "news": news_report, "fundamentals": fundamentals_report}
    settings = {  # shown in the report header
        "version": tradingagents.__version__, "llm_provider": "Claude Code",
        "deep_think_llm": "session model", "quick_think_llm": "session model",
        "analysts": [k for k, v in reports.items() if v.strip()],
        "max_debate_rounds": 1, "max_risk_discuss_rounds": 1,
    }
    md = write_report_tree(state, t, folder, settings=settings)
    extras = {"options_analysis": options_analysis, "options_strategy": options_strategy,
              "fact_check": fact_check}
    if any(v.strip() for v in extras.values()):
        _apply_extras(folder, t, extras, settings=settings)
    if log_decision:
        _memory().store_decision(t, trade_date, final_decision, rating=rating)
    return {"report_id": md.parent.name, "report_md": str(md),
            "report_html": str(md.with_suffix(".html")), "logged": log_decision}


def _reports_dir() -> Path:
    return Path(_config()["results_dir"]) / "reports"


def _report_dir(report_id: str) -> Path:
    """A saved report's folder, refusing ids that would leave the reports folder."""
    root = _reports_dir().resolve()
    folder = (root / report_id).resolve()
    if folder.parent != root or not re.fullmatch(r"[A-Za-z0-9._\-]+", report_id):
        raise ValueError(f"Not a report id: {report_id!r}. Use list_reports.")
    if not (folder / "complete_report.md").exists():
        raise ValueError(f"No report {report_id!r}. Use list_reports.")
    return folder


# Sections this plugin adds after TradingAgents' five: (heading, folder, [(agent, file, key)]).
EXTRA_SECTIONS = (
    ("VI. Options", "6_options", (("Options Analyst", "options.md", "options_analysis"),
                                  ("Options Strategist", "strategy.md", "options_strategy"))),
    ("VII. Fact Check", "7_fact_check", (("Fact Checker", "fact_check.md", "fact_check"),)),
)
_EXTRA_MARK = "\n\n<!-- tradingagents-claude: added sections -->\n\n"


def _apply_extras(folder: Path, ticker: str, texts: dict[str, str], settings: dict | None = None) -> None:
    """Write the added sections and rebuild complete_report.md/.html from the files on
    disk, so sections given earlier are kept and re-annotating replaces, never duplicates."""
    from tradingagents.report_html import render_report
    from tradingagents.reporting import _SECTIONS

    for _heading, sub, agents in EXTRA_SECTIONS:
        for _agent, filename, key in agents:
            if (texts.get(key) or "").strip():
                (folder / sub).mkdir(exist_ok=True)
                (folder / sub / filename).write_text(texts[key].strip() + "\n", encoding="utf-8")

    def from_disk(sections) -> list:
        parts = []
        for heading, sub, agents in sections:
            written = [(agent, filename, (folder / sub / filename).read_text(encoding="utf-8"))
                       for agent, filename, _ in agents if (folder / sub / filename).exists()]
            if written:
                parts.append((heading, sub, written))
        return parts

    base, extras = from_disk(_SECTIONS), from_disk(EXTRA_SECTIONS)
    md = folder / "complete_report.md"
    text = md.read_text(encoding="utf-8").split(_EXTRA_MARK)[0].rstrip()
    added = "\n\n".join(f"## {heading}\n\n" + "\n\n".join(f"### {a}\n{t}" for a, _, t in written)
                         for heading, _, written in extras)
    md.write_text(text + (_EXTRA_MARK + added if added else "") + "\n", encoding="utf-8")

    info = _report_header(md)
    decision = folder / "5_portfolio" / "decision.md"
    state = {"trade_date": info.get("trade_date", ""), "final_rating": info.get("rating"),
             "final_trade_decision": decision.read_text(encoding="utf-8") if decision.exists() else ""}
    html_path = folder / "complete_report.html"
    page = render_report(ticker, state, settings, base + extras)
    if settings is None and html_path.exists():
        # Keep the run details (models, analysts, vendors) the original page recorded.
        old = re.search(r'<dl class="run">.*?</dl>', html_path.read_text(encoding="utf-8"), re.S)
        if old:
            page = re.sub(r'<dl class="run">.*?</dl>', lambda _: old.group(0), page, count=1, flags=re.S)
    html_path.write_text(page, encoding="utf-8")


@mcp.tool()
@_quiet
def annotate_report(
    report_id: str,
    options_analysis: str = "",
    options_strategy: str = "",
    fact_check: str = "",
) -> dict:
    """Add or replace the Options and Fact Check sections of a saved report, such as
    one the API pipeline wrote (pipeline_report gives its report_id). Sections not
    passed are kept as they are. Updates both complete_report.md and .html."""
    folder = _report_dir(report_id)
    texts = {"options_analysis": options_analysis, "options_strategy": options_strategy,
             "fact_check": fact_check}
    if not any(v.strip() for v in texts.values()):
        raise ValueError("Pass at least one of options_analysis, options_strategy, fact_check.")
    ticker = _report_header(folder / "complete_report.md").get("ticker", report_id.split("_")[0])
    _apply_extras(folder, ticker, texts)
    return {"report_id": report_id, "report_html": str(folder / "complete_report.html"),
            "updated": [k for k, v in texts.items() if v.strip()]}


def _report_header(md: Path) -> dict:
    """Ticker, date, rating and generation time from a saved report's header lines."""
    info: dict[str, str] = {}
    with md.open(encoding="utf-8") as f:
        for _, line in zip(range(12), f):
            line = line.strip()
            if line.startswith("# Trading Analysis Report:"):
                info["ticker"] = line.split(":", 1)[1].strip()
            for label, key in (("- Analysis date:", "trade_date"), ("- Rating:", "rating"),
                               ("- Generated:", "generated"), ("- TradingAgents", "engine")):
                if line.startswith(label):
                    info[key] = line[len(label):].strip(" :")
    return info


@mcp.tool()
@_quiet
def list_reports(ticker: str | None = None, limit: int = 20) -> list[dict] | str:
    """List saved analysis reports, newest first, from both engines (the
    trading-analysis skill and the API pipeline), with ticker, date and rating.
    Read one with read_report."""
    root = _reports_dir()
    mds = sorted(root.glob("*/complete_report.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    t = _ticker(ticker) if ticker else None
    out = []
    for md in mds:
        info = _report_header(md)
        if t and info.get("ticker", "").upper() != t:
            continue
        out.append({"report_id": md.parent.name, **info})
        if len(out) >= limit:
            break
    return out or "No saved reports" + (f" for {t}." if t else ".")


@mcp.tool()
@_quiet
def read_report(report_id: str, max_chars: int = 60_000) -> str:
    """Read a saved report (from list_reports) in full: every analyst report, the
    bull/bear debate, the trader plan, the risk debate and the final decision."""
    md = _report_dir(report_id) / "complete_report.md"
    text = md.read_text(encoding="utf-8")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[truncated at {max_chars} characters; full report at {md}]"
    return text


@mcp.tool()
@_quiet
def backtest_results(run_id: str | None = None) -> list[dict] | str:
    """Backtests run with run_backtest. Without run_id, lists them with their cell
    counts. With run_id, returns the scorecard: per rating, how often it called the
    direction and its mean alpha against the benchmark. Reading costs nothing; to
    settle cells whose window has since traded, run run_backtest again with the run_id."""
    from tradingagents.backtest import summarize
    from tradingagents.memory.log import TradingMemoryLog

    root = Path(_config()["results_dir"]) / "backtest"
    if run_id:
        if not re.fullmatch(r"[A-Za-z0-9._\-]+", run_id):
            raise ValueError(f"Not a run id: {run_id!r}")
        log = root / run_id / "trading_memory.md"
        if not log.exists():
            raise ValueError(f"No backtest {run_id!r}. Call backtest_results without run_id to list them.")
        entries = TradingMemoryLog({"memory_log_path": str(log)}).load_entries()
        cells = sorted({(e["ticker"], e["date"]) for e in entries})
        return (f"Backtest {run_id}: {len(cells)} cells, tickers "
                f"{', '.join(sorted({c[0] for c in cells}))}, dates {cells[0][1]} to {cells[-1][1]}\n\n"
                + summarize(log).render()) if cells else f"Backtest {run_id} has no finished cells."
    out = []
    for log in sorted(root.glob("*/trading_memory.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        entries = TradingMemoryLog({"memory_log_path": str(log)}).load_entries()
        out.append({"run_id": log.parent.name, "cells": len(entries),
                    "pending": sum(1 for e in entries if e.get("pending")),
                    "tickers": sorted({e["ticker"] for e in entries}),
                    "updated": datetime.fromtimestamp(log.stat().st_mtime).isoformat(timespec="minutes")})
    return out or "No backtests yet."


# ---------------------------------------------------------------- entry point

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    import pipeline

    if pipeline.keys_present():
        pipeline.register(mcp)
        print("tradingagents: API key found, pipeline tools enabled", file=sys.stderr)

    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()

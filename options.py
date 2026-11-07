"""Listed options: live chains from Yahoo Finance and deterministic options math.

Everything an agent states about options should come from here, not from the
model's arithmetic: implied move, implied volatility (IV), skew, put/call ratios,
open-interest walls, max pain, Black-Scholes deltas, and strategy payoffs.

Yahoo serves only the current chain (no history), so these figures exist for
today only, and mostly for US listings; there is nothing to backtest.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime
from statistics import NormalDist

import pandas as pd

_N = NormalDist()
CONTRACT = 100  # shares per standard US equity option
IV_MIN, IV_MAX = 0.01, 5.0  # Yahoo reports junk IVs for dead strikes; ignore outside this


def risk_free_rate() -> float:
    return float(os.getenv("TA_RISK_FREE_RATE", "0.04"))


# ---------------------------------------------------------------- Black-Scholes

def years_to(expiry: str, today: date | None = None) -> float:
    """Year fraction to expiry; at least half a day so same-day expiries stay finite."""
    days = (date.fromisoformat(expiry) - (today or date.today())).days
    return max(days, 0.5) / 365.0


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return d1, d1 - sigma * math.sqrt(T)


def bs_delta(kind: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return _N.cdf(d1) if kind == "call" else _N.cdf(d1) - 1.0


def prob_above(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Risk-neutral probability that the price ends above K at expiry (N(d2))."""
    if K <= 0:
        return 1.0
    _, d2 = _d1_d2(S, K, T, r, sigma)
    return _N.cdf(d2)


# ---------------------------------------------------------------- Yahoo

def _yahoo(fn):
    from tradingagents.dataflows.vendors.yahoo.common import yf_retry

    return yf_retry(fn)


def expirations(symbol: str) -> list[str]:
    import yfinance as yf

    return list(_yahoo(lambda: yf.Ticker(symbol).options) or [])


def fetch_chain(symbol: str, expiry: str) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """(calls, puts, spot) for one expiry, with mid prices and usable IVs marked."""
    import yfinance as yf

    def get():
        t = yf.Ticker(symbol)
        ch = t.option_chain(expiry)
        spot = (getattr(ch, "underlying", None) or {}).get("regularMarketPrice") \
            or t.fast_info["last_price"]
        return ch.calls, ch.puts, float(spot)

    out = _yahoo(get)
    if out is None:
        raise ValueError(f"Yahoo has no {expiry} option chain for {symbol}")
    calls, puts, spot = out
    return _clean(calls), _clean(puts), spot


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in ("bid", "ask", "lastPrice", "volume", "openInterest", "impliedVolatility"):
        df[col] = pd.to_numeric(df.get(col), errors="coerce").fillna(0.0)
    quoted = (df["bid"] > 0) & (df["ask"] >= df["bid"])
    df["mid"] = ((df["bid"] + df["ask"]) / 2).where(quoted)
    df["price"] = df["mid"].fillna(df["lastPrice"].where(df["lastPrice"] > 0))
    df["priced_from"] = ["mid" if q else "last" for q in quoted]
    df["iv"] = df["impliedVolatility"].where(df["impliedVolatility"].between(IV_MIN, IV_MAX))
    return df.sort_values("strike").reset_index(drop=True)


def realized_vols(symbol: str, windows=(20, 60)) -> dict[int, float | None]:
    """Annualised close-to-close volatility over the last N sessions, per window."""
    import yfinance as yf

    hist = _yahoo(lambda: yf.Ticker(symbol).history(period="6mo", auto_adjust=True))
    closes = hist["Close"] if hist is not None else pd.Series(dtype=float)
    return {w: realized_vol_from(closes, w) for w in windows}


def realized_vol_from(closes: pd.Series, days: int) -> float | None:
    if len(closes) < days + 1:
        return None
    rets = (closes / closes.shift(1)).apply(math.log).dropna().tail(days)
    return float(rets.std() * math.sqrt(252))


def next_earnings(symbol: str) -> str | None:
    import yfinance as yf

    try:
        cal = _yahoo(lambda: yf.Ticker(symbol).calendar) or {}
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        upcoming = [d for d in (dates or []) if d >= date.today()]
        return min(upcoming).isoformat() if upcoming else None
    except Exception:  # noqa: BLE001 - the calendar is a nice-to-have
        return None


# ---------------------------------------------------------------- chain analytics

@dataclass
class ExpirySummary:
    expiry: str
    dte: int
    spot: float
    atm_strike: float
    atm_iv: float | None
    straddle: float | None
    implied_move: float | None  # fraction of spot
    rr25: float | None  # IV(25-delta put) - IV(25-delta call), in vol points
    pc_volume: float | None
    pc_oi: float | None
    max_pain: float | None
    call_walls: list[tuple[float, int]]
    put_walls: list[tuple[float, int]]


def _nearest(df: pd.DataFrame, strike: float) -> pd.Series | None:
    if df.empty:
        return None
    return df.iloc[(df["strike"] - strike).abs().argsort().iloc[0]]


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 2) if b > 0 else None


def max_pain(calls: pd.DataFrame, puts: pd.DataFrame) -> float | None:
    """The strike at which option holders' total payout at expiry is smallest."""
    strikes = sorted(set(calls["strike"]) | set(puts["strike"]))
    if not strikes or (calls["openInterest"].sum() + puts["openInterest"].sum()) == 0:
        return None

    def payout(k: float) -> float:
        return float((calls["openInterest"] * (k - calls["strike"]).clip(lower=0)).sum()
                     + (puts["openInterest"] * (puts["strike"] - k).clip(lower=0)).sum())

    return min(strikes, key=payout)


def with_deltas(df: pd.DataFrame, kind: str, spot: float, T: float, r: float) -> pd.DataFrame:
    df = df.copy()
    df["delta"] = [bs_delta(kind, spot, k, T, r, iv) if pd.notna(iv) else float("nan")
                   for k, iv in zip(df["strike"], df["iv"])]
    return df


def summarize_expiry(calls: pd.DataFrame, puts: pd.DataFrame, spot: float, expiry: str,
                     today: date | None = None) -> ExpirySummary:
    today = today or date.today()
    T, r = years_to(expiry, today), risk_free_rate()
    common = sorted(set(calls["strike"]) & set(puts["strike"]))
    atm = min(common, key=lambda k: abs(k - spot)) if common else spot
    c, p = _nearest(calls, atm), _nearest(puts, atm)
    straddle = (c["price"] + p["price"]) if c is not None and p is not None \
        and pd.notna(c["price"]) and pd.notna(p["price"]) else None
    ivs = [x["iv"] for x in (c, p) if x is not None and pd.notna(x["iv"])]
    atm_iv = sum(ivs) / len(ivs) if ivs else None

    rr25 = None
    cd, pdl = with_deltas(calls, "call", spot, T, r), with_deltas(puts, "put", spot, T, r)
    cd, pdl = cd.dropna(subset=["delta"]), pdl.dropna(subset=["delta"])
    if not cd.empty and not pdl.empty:
        c25 = cd.iloc[(cd["delta"] - 0.25).abs().argsort().iloc[0]]
        p25 = pdl.iloc[(pdl["delta"] + 0.25).abs().argsort().iloc[0]]
        if abs(c25["delta"] - 0.25) < 0.1 and abs(p25["delta"] + 0.25) < 0.1:
            rr25 = round((p25["iv"] - c25["iv"]) * 100, 1)

    def walls(df: pd.DataFrame) -> list[tuple[float, int]]:
        top = df[df["openInterest"] > 0].nlargest(3, "openInterest")
        return [(float(k), int(oi)) for k, oi in zip(top["strike"], top["openInterest"])]

    return ExpirySummary(
        expiry=expiry, dte=(date.fromisoformat(expiry) - today).days, spot=spot,
        atm_strike=float(atm), atm_iv=atm_iv, straddle=straddle,
        implied_move=(straddle / spot) if straddle else None, rr25=rr25,
        pc_volume=_ratio(puts["volume"].sum(), calls["volume"].sum()),
        pc_oi=_ratio(puts["openInterest"].sum(), calls["openInterest"].sum()),
        max_pain=max_pain(calls, puts), call_walls=walls(calls), put_walls=walls(puts))


def pick_expiries(all_expiries: list[str], targets=(7, 30, 60, 90, 180), today: date | None = None) -> list[str]:
    """The listed expiries nearest each target horizon in days, plus the first one."""
    today = today or date.today()
    if not all_expiries:
        return []
    dte = {e: (date.fromisoformat(e) - today).days for e in all_expiries}
    chosen = {all_expiries[0]}
    for t in targets:
        chosen.add(min(all_expiries, key=lambda e: abs(dte[e] - t)))
    return sorted(chosen)


def default_expiry(all_expiries: list[str], min_dte: int = 21, today: date | None = None) -> str:
    today = today or date.today()
    for e in all_expiries:
        if (date.fromisoformat(e) - today).days >= min_dte:
            return e
    return all_expiries[-1]


# ---------------------------------------------------------------- strategies

@dataclass
class Leg:
    kind: str  # call | put | stock
    side: str  # buy | sell
    quantity: float  # contracts for options, shares for stock
    strike: float | None = None
    price: float = 0.0  # per share (option premium per share, or stock price)
    iv: float | None = None

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1

    def payoff(self, s: float) -> float:
        """Value at expiry, per position, in currency."""
        if self.kind == "stock":
            return self.sign * self.quantity * s
        intrinsic = max(0.0, s - self.strike) if self.kind == "call" else max(0.0, self.strike - s)
        return self.sign * self.quantity * CONTRACT * intrinsic

    @property
    def cost(self) -> float:
        """Cash paid to open (negative when received)."""
        mult = 1 if self.kind == "stock" else CONTRACT
        return self.sign * self.quantity * mult * self.price


def analyze_strategy(legs: list[Leg], spot: float, expiry: str | None, sigma: float | None,
                     today: date | None = None) -> dict:
    """Payoff at expiry: net cost, max profit/loss, breakevens, probability of profit."""
    strikes = [l.strike for l in legs if l.strike]
    hi = max([spot] + strikes) * 3
    grid = sorted(set([0.0, hi] + strikes + [spot] + [hi * i / 3000 for i in range(1, 3000)]))
    net_cost = sum(l.cost for l in legs)
    pnl = [sum(l.payoff(s) for l in legs) - net_cost for s in grid]

    # Slope above the highest strike: + per share held, + per long call, - per short call.
    up_slope = sum(l.sign * (l.quantity if l.kind == "stock" else l.quantity * CONTRACT)
                   for l in legs if l.kind in ("stock", "call"))
    max_profit = None if up_slope > 0 else max(pnl)
    max_loss = None if up_slope < 0 else min(pnl)

    breakevens = []
    for (s0, p0), (s1, p1) in zip(zip(grid, pnl), zip(grid[1:], pnl[1:])):
        if p0 == 0 and s0 > 0:
            breakevens.append(s0)
        elif p0 * p1 < 0:
            breakevens.append(s0 + (s1 - s0) * (-p0) / (p1 - p0))
    breakevens = sorted({round(b, 2) for b in breakevens})

    pop = None
    if expiry and sigma:
        T, r = years_to(expiry, today), risk_free_rate()
        # Probability mass of the price intervals where P/L > 0, under a lognormal.
        edges = [0.0] + breakevens + [float("inf")]
        pop = 0.0
        for a, b in zip(edges, edges[1:]):
            mid = (a + b) / 2 if b != float("inf") else a * 1.5 + 1
            if sum(l.payoff(mid) for l in legs) - net_cost > 0:
                pa = prob_above(spot, a, T, r, sigma) if a > 0 else 1.0
                pb = prob_above(spot, b, T, r, sigma) if b != float("inf") else 0.0
                pop += pa - pb
        pop = round(pop, 3)

    net_delta = None
    if expiry:
        T, r = years_to(expiry, today), risk_free_rate()
        try:
            net_delta = round(sum(
                l.sign * l.quantity if l.kind == "stock"
                else l.sign * l.quantity * CONTRACT * bs_delta(l.kind, spot, l.strike, T, r, l.iv or sigma)
                for l in legs), 1)
        except (TypeError, ValueError, ZeroDivisionError):
            net_delta = None

    return {
        "net_cost": round(net_cost, 2),
        "max_profit": None if max_profit is None else round(max_profit, 2),
        "max_loss": None if max_loss is None else round(max_loss, 2),
        "profit_unbounded": up_slope > 0,
        "loss_unbounded": up_slope < 0,
        "breakevens": breakevens,
        "probability_of_profit": pop,
        "net_delta_shares": net_delta,
    }

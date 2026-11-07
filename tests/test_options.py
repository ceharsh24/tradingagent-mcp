"""Offline tests of the options math in options.py (no network)."""
import math
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import options as opt  # noqa: E402

TODAY = date(2026, 11, 7)


def chain(rows):
    """rows: (strike, bid, ask, iv, volume, oi)."""
    df = pd.DataFrame(rows, columns=["strike", "bid", "ask", "impliedVolatility", "volume", "openInterest"])
    df["lastPrice"] = (df["bid"] + df["ask"]) / 2
    return opt._clean(df)


def test_black_scholes_reference_values():
    # Hull's textbook case: S=42, K=40, r=10%, sigma=20%, T=0.5 -> call delta 0.7791
    assert opt.bs_delta("call", 42, 40, 0.5, 0.10, 0.20) == pytest.approx(0.7791, abs=1e-4)
    assert opt.bs_delta("put", 42, 40, 0.5, 0.10, 0.20) == pytest.approx(0.7791 - 1, abs=1e-4)
    assert opt.prob_above(100, 100, 1.0, 0.0, 0.2) == pytest.approx(0.4602, abs=1e-4)


def test_years_to_and_expiry_picking():
    assert opt.years_to("2026-12-07", TODAY) == pytest.approx(30 / 365)
    assert opt.years_to("2026-11-07", TODAY) == pytest.approx(0.5 / 365)  # same-day stays finite
    exps = ["2026-11-09", "2026-11-13", "2026-11-20", "2026-12-04", "2026-12-18", "2027-01-15", "2027-05-21"]
    assert opt.default_expiry(exps, today=TODAY) == "2026-12-04"
    picked = opt.pick_expiries(exps, today=TODAY)
    assert picked[0] == "2026-11-09" and "2026-12-04" in picked and "2027-05-21" in picked


def test_clean_marks_quotes_and_junk_iv():
    df = chain([(100, 0, 0, 0.00001, 0, 0), (105, 2.0, 2.2, 0.3, 10, 50)])
    assert math.isnan(df.loc[0, "iv"]) and df.loc[0, "priced_from"] == "last"
    assert df.loc[1, "mid"] == pytest.approx(2.1) and df.loc[1, "priced_from"] == "mid"


def test_summarize_expiry_on_synthetic_chain():
    calls = chain([(90, 10.9, 11.1, 0.32, 5, 100), (100, 3.9, 4.1, 0.30, 50, 300), (110, 0.9, 1.1, 0.28, 20, 900)])
    puts = chain([(90, 0.9, 1.1, 0.36, 40, 800), (100, 3.9, 4.1, 0.31, 30, 400), (110, 10.9, 11.1, 0.29, 5, 50)])
    x = opt.summarize_expiry(calls, puts, 100.0, "2026-12-07", today=TODAY)
    assert x.atm_strike == 100 and x.straddle == pytest.approx(8.0) and x.implied_move == pytest.approx(0.08)
    assert x.atm_iv == pytest.approx(0.305)
    assert x.pc_oi == pytest.approx(1250 / 1300, abs=0.01) and x.pc_volume == pytest.approx(75 / 75)
    assert x.call_walls[0] == (110.0, 900) and x.put_walls[0] == (90.0, 800)
    # Max pain: payout at 100 = calls 90*100*10 + puts 110*50*10 = 1500, the minimum here.
    assert x.max_pain == 100


def leg(kind, side, strike=None, price=0.0, qty=1, iv=0.3):
    return opt.Leg(kind, side, qty, strike, price, iv)


def test_bull_call_spread():
    r = opt.analyze_strategy([leg("call", "buy", 100, 4.0), leg("call", "sell", 110, 1.0)],
                             100.0, "2026-12-07", 0.3, today=TODAY)
    assert r["net_cost"] == 300 and r["max_profit"] == 700 and r["max_loss"] == -300
    assert r["breakevens"] == [103.0] and not r["profit_unbounded"] and not r["loss_unbounded"]
    assert 0 < r["probability_of_profit"] < 0.5


def test_covered_call_and_short_put_and_naked_call():
    cc = opt.analyze_strategy([leg("stock", "buy", price=100, qty=100), leg("call", "sell", 110, 2.0)],
                              100.0, "2026-12-07", 0.3, today=TODAY)
    assert cc["max_profit"] == 1200 and cc["max_loss"] == -9800 and cc["breakevens"] == [98.0]
    sp = opt.analyze_strategy([leg("put", "sell", 95, 2.5)], 100.0, "2026-12-07", 0.3, today=TODAY)
    assert sp["net_cost"] == -250 and sp["max_profit"] == 250 and sp["max_loss"] == -9250
    assert sp["breakevens"] == [92.5] and sp["probability_of_profit"] > 0.5
    nc = opt.analyze_strategy([leg("call", "sell", 110, 1.0)], 100.0, "2026-12-07", 0.3, today=TODAY)
    assert nc["loss_unbounded"] and nc["max_loss"] is None and nc["max_profit"] == 100


def test_long_straddle_two_breakevens_and_pop():
    r = opt.analyze_strategy([leg("call", "buy", 100, 4.0), leg("put", "buy", 100, 4.0)],
                             100.0, "2026-12-07", 0.3, today=TODAY)
    assert r["breakevens"] == [92.0, 108.0] and r["profit_unbounded"] and r["max_loss"] == -800
    # P(S<92) + P(S>108) under the lognormal
    T = 30 / 365
    expected = (1 - opt.prob_above(100, 92, T, 0.04, 0.3)) + opt.prob_above(100, 108, T, 0.04, 0.3)
    assert r["probability_of_profit"] == pytest.approx(expected, abs=0.002)


def test_realized_vol_from_constant_growth_is_zero():
    closes = pd.Series([100 * 1.01 ** i for i in range(30)])
    assert opt.realized_vol_from(closes, 20) == pytest.approx(0.0, abs=1e-9)
    assert opt.realized_vol_from(closes, 40) is None

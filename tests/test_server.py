"""Offline tests of the data/memory/report tools in server.py."""
import os, sys, tempfile
from pathlib import Path

import pandas as pd
import pytest

TMP = Path(tempfile.mkdtemp())
os.environ["TRADINGAGENTS_MEMORY_LOG_PATH"] = str(TMP / "memory.md")
os.environ["TRADINGAGENTS_RESULTS_DIR"] = str(TMP / "results")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


def test_ticker_and_date_validation():
    assert server._ticker(" nvda ") == "NVDA"
    with pytest.raises(ValueError):
        server._ticker("../etc/passwd")
    with pytest.raises(ValueError):
        server._as_of("2999-01-01")
    with pytest.raises(ValueError):
        server._as_of("11/06/2026")


def test_vendor_failure_becomes_text(monkeypatch):
    def boom(*a):
        raise RuntimeError("yahoo down")
    monkeypatch.setattr(server, "_route", boom)
    assert "unavailable" in server.fundamentals("NVDA", "2026-11-06")


def test_tools_pass_dates_through(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_route", lambda *a: seen.append(a) or "ok")
    server.company_news("NVDA", "2026-11-06", days=7)
    server.financial_statement("NVDA", "cashflow", "annual", "2026-11-06")
    server.technical_indicators("NVDA", ["RSI", "rsi", "macd"], "2026-11-06", 10)
    assert seen[0] == ("get_news", "NVDA", "2026-10-30", "2026-11-06")
    assert seen[1] == ("get_cashflow", "NVDA", "annual", "2026-11-06")
    assert [a[2] for a in seen[2:]] == ["rsi", "macd"]  # deduped, lower-cased


def test_save_settle_and_lessons(monkeypatch):
    out = server.save_analysis(
        "NVDA", "2026-10-01", "Overweight", "**Rating**: Overweight\n\nBuy dips.",
        market_report="Trend up.", bull_case="Bull.", bear_case="Bear.",
        trader_plan="FINAL TRANSACTION PROPOSAL: **BUY**")
    md, html = Path(out["report_md"]), Path(out["report_html"])
    assert md.exists() and html.exists()
    text = md.read_text()
    assert "Rating: Overweight" in text and "Trend up." in text and "Bull." in text
    assert "[2026-10-01 | NVDA | Overweight | pending]" in server.past_decisions("NVDA")

    import tradingagents.memory.settlement as st
    monkeypatch.setattr(st, "fetch_returns", lambda *a, **k: (0.05, 0.02, 5, "2026-10-08"))
    ready = server.unsettled_decisions()
    assert ready[0]["status"] == "ready" and ready[0]["alpha"] == 0.02
    msg = server.settle_decision("NVDA", "2026-10-01", "Right call; momentum held.")
    assert msg.startswith("Settled NVDA") and "+2.00%" in msg
    assert server.unsettled_decisions() == "No unsettled decisions."
    assert "momentum held" in server.memory_lessons("NVDA", "2026-11-06")
    assert "No logged" in server.past_decisions("TSLA")


def test_settle_waits_for_window(monkeypatch):
    import tradingagents.memory.settlement as st
    monkeypatch.setattr(st, "fetch_returns", lambda *a, **k: (None, None, None, None))
    assert "not traded yet" in server.settle_decision("AAPL", "2026-11-06", "x")


def test_list_and_read_reports():
    out = server.save_analysis("AAPL", "2026-11-06", "Hold", "**Rating**: Hold\n\nWait.",
                               news_report="Quiet week.", log_decision=False)
    reports = server.list_reports("aapl")
    assert reports[0]["report_id"] == out["report_id"]
    assert reports[0]["rating"] == "Hold" and reports[0]["trade_date"] == "2026-11-06"
    assert "Quiet week." in server.read_report(out["report_id"])
    assert "No saved reports for MSFT" in server.list_reports("MSFT")
    for bad in ("../memory", "..", "x/../../etc", "nope"):
        with pytest.raises(ValueError):
            server.read_report(bad)


def test_backtest_results():
    from tradingagents.memory.log import TradingMemoryLog
    assert server.backtest_results() == "No backtests yet."
    log = TMP / "results" / "backtest" / "run1" / "trading_memory.md"
    log.parent.mkdir(parents=True)
    mem = TradingMemoryLog({"memory_log_path": str(log)})
    mem.store_decision("NVDA", "2026-09-01", "**Rating**: Buy", rating="Buy")
    mem.store_decision("NVDA", "2026-09-08", "**Rating**: Sell", rating="Sell")
    mem.update_with_outcome("NVDA", "2026-09-01", 0.04, 0.03, 5, "Good call.", resolution_date="2026-09-08")
    runs = server.backtest_results()
    assert runs[0]["run_id"] == "run1" and runs[0]["cells"] == 2 and runs[0]["pending"] == 1
    card = server.backtest_results("run1")
    assert "2 cells" in card and "Buy: n=1" in card and "+3.00%" in card
    with pytest.raises(ValueError):
        server.backtest_results("../x")


def test_extra_sections_save_and_annotate():
    out = server.save_analysis("MSFT", "2026-11-06", "Buy", "**Rating**: Buy\n\nAdd on dips.",
                               market_report="Uptrend.", options_analysis="IV 30%, implied move ±6%.",
                               fact_check="| claim | status |\n|---|---|\n| RSI 55 | verified |",
                               log_decision=False)
    folder = Path(out["report_md"]).parent
    md, html = (folder / "complete_report.md").read_text(), (folder / "complete_report.html").read_text()
    assert "## VI. Options" in md and "### Options Analyst" in md and "## VII. Fact Check" in md
    assert "VI. Options" in html and "Fact Checker" in html and "Claude Code" in html
    # annotate: replaces one section, keeps the others, never duplicates, keeps run details
    server.annotate_report(out["report_id"], options_strategy="Bull call spread 400/420.",
                           fact_check="All figures verified.")
    md2, html2 = (folder / "complete_report.md").read_text(), (folder / "complete_report.html").read_text()
    assert md2.count("## VI. Options") == 1 and md2.count("## VII. Fact Check") == 1
    assert "IV 30%" in md2 and "Bull call spread" in md2 and "All figures verified." in md2
    assert "RSI 55" not in md2 and "Uptrend." in md2
    assert "Options Strategist" in html2 and "Claude Code" in html2
    with pytest.raises(ValueError):
        server.annotate_report(out["report_id"])
    with pytest.raises(ValueError):
        server.annotate_report("../x", fact_check="y")


def test_options_tools_refuse_past_dates_and_unlisted(monkeypatch):
    import options as opt
    assert "withheld for 2026-11-01" in server.options_chain("NVDA", as_of="2026-11-01")
    monkeypatch.setattr(opt, "expirations", lambda t: [])
    assert "No listed options for RELIANCE.NS" in server.options_overview("RELIANCE.NS")

"""In-process tests of pipeline.py with TradingAgents' graph and backtest replaced by fakes."""
import os, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ["TA_MCP_JOBS_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline  # noqa: E402
import tradingagents.backtest as tb  # noqa: E402
import tradingagents.graph.trading_graph as tg  # noqa: E402


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")


class FakeGraph:
    last = None

    def __init__(self, selected_analysts=("market",), config=None, callbacks=None):
        self.cb = (callbacks or [None])[0]
        self.analysts, self.config, self.cleared = selected_analysts, config, False
        FakeGraph.last = self

    def clear_checkpoint_on_success(self, *args):
        self.cleared = True

    def propagate(self, ticker, date, asset_type, portfolio):
        for ns in ("Market Analyst:1|agent", "Bull Researcher:2", ""):
            self.cb.on_chat_model_start({}, [], metadata={"langgraph_checkpoint_ns": ns,
                                                          "langgraph_node": "Portfolio Manager"})
        print("noise on stdout")  # must not reach the MCP stream
        self.asset_type, self.portfolio = asset_type, portfolio
        return {"final_trade_decision": f"Rating: Overweight on {ticker}"}, "Overweight"

    def save_reports(self, state, ticker):
        d = Path(tempfile.mkdtemp()) / f"{ticker}_20261107_100000"
        d.mkdir()
        (d / "complete_report.md").write_text("# full " + ticker)
        return d / "complete_report.md"

    def settle_all_pending(self):
        return SimpleNamespace(settled=[("NVDA", "2026-10-01")],
                               failed=[("AAPL", "2026-10-02", "prices unavailable")])


def wait(job_id):
    for _ in range(200):
        st = pipeline.pipeline_status(job_id)
        if st["status"] not in ("queued", "running"):
            return st
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_analysis_happy_path(monkeypatch):
    monkeypatch.setattr(tg, "TradingAgentsGraph", FakeGraph)
    job = pipeline.run_pipeline("btc-usd", "2026-11-06", ["market", "market", "news"],
                                {"cash": 1000, "currency": "USD", "positions": []})
    st = wait(job["job_id"])
    assert st["status"] == "done" and st["kind"] == "analysis"
    assert st["signal"] == "Overweight" and st["llm_calls"] == 3
    assert st["analysts"] == ["market", "news"]
    g = FakeGraph.last
    assert g.asset_type == "crypto" and g.portfolio is not None
    assert g.config["checkpoint_enabled"] is True and g.cleared is False
    rep = pipeline.pipeline_report(job["job_id"], "full")
    assert rep["decision"].endswith("BTC-USD") and rep["full_report"] == "# full BTC-USD"
    assert rep["report_id"] == "BTC-USD_20261107_100000"
    assert "full_report" not in pipeline.pipeline_report(job["job_id"])


def test_per_run_settings_and_restart(monkeypatch):
    monkeypatch.setattr(tg, "TradingAgentsGraph", FakeGraph)
    job = pipeline.run_pipeline("NVDA", "2026-11-06", debate_rounds=2, risk_rounds=3,
                                quick_model="claude-sonnet-5", deep_model="claude-opus-5-5",
                                output_language="Hindi", restart=True)
    assert job["settings"]["debate_rounds"] == 2 and job["settings"]["language"] == "Hindi"
    wait(job["job_id"])
    cfg = FakeGraph.last.config
    assert (cfg["max_debate_rounds"], cfg["max_risk_discuss_rounds"]) == (2, 3)
    assert (cfg["quick_think_llm"], cfg["deep_think_llm"]) == ("claude-sonnet-5", "claude-opus-5-5")
    assert FakeGraph.last.cleared is True
    with pytest.raises(ValueError, match="debate_rounds"):
        pipeline.run_pipeline("NVDA", debate_rounds=5)


def test_failure_and_interrupted(monkeypatch):
    class Boom(FakeGraph):
        def propagate(self, *a, **k):
            raise RuntimeError("rate limited")
    monkeypatch.setattr(tg, "TradingAgentsGraph", Boom)
    st = wait(pipeline.run_pipeline("NVDA", "2026-11-06")["job_id"])
    assert st["status"] == "failed" and "rate limited" in st["error"]
    # jobs left running by a dead process are marked interrupted, with how to resume
    pipeline._save({"id": "dead1", "status": "running", "created_at": "2026-01-01"})
    pipeline._save({"id": "dead2", "kind": "backtest", "status": "running", "created_at": "2026-01-01"})
    pipeline._load_jobs()
    assert "resumes" in pipeline.pipeline_status("dead1")["error"]
    assert pipeline.pipeline_status("dead1")["kind"] == "analysis"  # pre-kind record
    assert "run_id" in pipeline.pipeline_status("dead2")["error"]


def test_backtest_cost_guard():
    with pytest.raises(ValueError, match=r"12 cells \(2 tickers x 6 dates\)"):
        pipeline.run_backtest(["NVDA", "AAPL"], "2026-09-01", "2026-10-06", every_n_days=7, max_cells=10)
    with pytest.raises(ValueError, match="separately"):
        pipeline.run_backtest(["NVDA", "BTC-USD"], "2026-09-01", "2026-09-01")
    with pytest.raises(ValueError, match="max_cells"):
        pipeline.run_backtest(["NVDA"], "2026-09-01", "2026-09-01", max_cells=1000)


def test_backtest_job(monkeypatch):
    calls = {}

    def fake_backtest(tickers, dates, config, asset_type, portfolio, selected_analysts, run_id, progress):
        calls.update(tickers=tickers, dates=dates, asset_type=asset_type, run_id=run_id,
                     analysts=selected_analysts, debate=config["max_debate_rounds"])
        for i, (t, d) in enumerate([(t, d) for t in tickers for d in dates], 1):
            progress(i, len(tickers) * len(dates), t, d)
        return SimpleNamespace(run_id="20261107_1", log_path=Path("/tmp/x.md"), cells_run=3,
                               skipped=1, failures=[("AAPL", "2026-09-08", "yahoo down")],
                               settlement_failures=[])

    monkeypatch.setattr(tb, "run_backtest", fake_backtest)
    monkeypatch.setattr(tb, "summarize", lambda r: SimpleNamespace(render=lambda: "- Buy: n=2"))
    job = pipeline.run_backtest(["nvda", "AAPL"], "2026-09-01", "2026-09-08",
                                analysts=["market"], debate_rounds=2, run_id="resume-me")
    assert job["cells"] == 4 and job["dates"] == ["2026-09-01", "2026-09-08"]
    st = wait(job["job_id"])
    assert st["status"] == "done" and st["kind"] == "backtest"
    assert calls == {"tickers": ["NVDA", "AAPL"], "dates": ["2026-09-01", "2026-09-08"],
                     "asset_type": "stock", "run_id": "resume-me", "analysts": ("market",), "debate": 2}
    rep = pipeline.pipeline_report(job["job_id"])
    assert rep["summary"] == "- Buy: n=2" and rep["run_id"] == "20261107_1"
    assert rep["failures"] == ["AAPL 2026-09-08: yahoo down"] and rep["cells_skipped"] == 1


def test_settle_job(monkeypatch):
    monkeypatch.setattr(tg, "TradingAgentsGraph", FakeGraph)
    job = pipeline.pipeline_settle()
    assert wait(job["job_id"])["status"] == "done"
    rep = pipeline.pipeline_report(job["job_id"])
    assert rep == {"kind": "settle", "settled": ["NVDA 2026-10-01"],
                   "failures": ["AAPL 2026-10-02: prices unavailable"]}
    assert all(j["kind"] == "settle" for j in pipeline.pipeline_jobs(kind="settle"))


def test_stage_names():
    pipeline._jobs["x"] = {"id": "x"}
    p = pipeline._Progress("x")
    p.on_chat_model_start({}, [], metadata={"langgraph_checkpoint_ns": "Market Analyst:abc|agent",
                                            "langgraph_node": "agent"})
    assert pipeline._jobs["x"]["current_stage"] == "Market Analyst"
    p.on_chat_model_start({}, [], metadata={"langgraph_node": "Portfolio Manager"})
    assert pipeline._jobs["x"]["current_stage"] == "Portfolio Manager"


def test_bad_inputs():
    with pytest.raises(ValueError):
        pipeline.run_pipeline("NVDA", "11/06/2026")
    with pytest.raises(ValueError):
        pipeline.pipeline_status("nope")


def test_missing_key_fails_fast(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is not set"):
        pipeline.run_pipeline("NVDA")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is not set"):
        pipeline.pipeline_settle()
    assert pipeline.keys_present() is False


def test_pipeline_wait_returns_report_or_status(monkeypatch):
    import anyio
    monkeypatch.setattr(tg, "TradingAgentsGraph", FakeGraph)
    job = pipeline.run_pipeline("NVDA", "2026-11-06")
    rep = anyio.run(lambda: pipeline.pipeline_wait(job["job_id"], timeout_s=30))
    assert rep["signal"] == "Overweight" and rep["kind"] == "analysis"
    pipeline._jobs["slow"] = {"id": "slow", "status": "running", "started_at": "2026-11-07T10:00:00"}
    st = anyio.run(lambda: pipeline.pipeline_wait("slow", timeout_s=10))
    assert st["status"] == "running" and "call pipeline_wait again" in st["note"]
    with pytest.raises(ValueError):
        anyio.run(lambda: pipeline.pipeline_wait("slow", timeout_s=5))


def test_sleep_guard(monkeypatch):
    started, stopped = [], []

    class FakeProc:
        def __init__(self, args, **kw):
            started.append(args)
        def poll(self):
            return None
        def terminate(self):
            stopped.append(True)

    monkeypatch.setattr(pipeline.shutil, "which", lambda name: "/usr/bin/caffeinate")
    monkeypatch.setattr(pipeline.subprocess, "Popen", FakeProc)
    guard = pipeline._Awake()
    guard.hold(+1); guard.hold(+1)
    assert len(started) == 1 and started[0][:2] == ["caffeinate", "-is"] and guard.holding
    guard.hold(-1)
    assert not stopped
    guard.hold(-1)
    assert stopped == [True] and not guard.holding
    monkeypatch.setenv("TA_MCP_KEEP_AWAKE", "0")
    guard.hold(+1)
    assert len(started) == 1  # disabled: no new caffeinate


def test_chatgpt_provider_needs_sign_in(monkeypatch, tmp_path):
    import tradingagents.llm_clients.chatgpt_auth as auth
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "chatgpt")
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "chatgpt-auth.json")
    with pytest.raises(ValueError, match="login chatgpt"):
        pipeline.run_pipeline("NVDA")
    assert pipeline.keys_present() is False
    (tmp_path / "chatgpt-auth.json").write_text("{}")
    assert pipeline.keys_present() is True

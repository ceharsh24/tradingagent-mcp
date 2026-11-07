"""Optional tools that run the original TradingAgents LangGraph pipeline.

The pipeline makes its own model calls, so these tools are registered only when
an API key for the configured provider is set (see server.py). Work takes
minutes, so every tool starts a background job and returns its id at once;
pipeline_status and pipeline_report check on it. Job records persist in
~/.tradingagents/mcp_jobs.

Three kinds of job:
* analysis  run_pipeline: one ticker on one date
* backtest  run_backtest: a grid of tickers x dates, scored by rating
* settle    pipeline_settle: score past decisions and write their reflections
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Literal

import anyio
from langchain_core.callbacks import BaseCallbackHandler
from mcp.server.mcpserver import Context

JOBS_DIR = Path(
    os.getenv("TA_MCP_JOBS_DIR") or Path.home() / ".tradingagents" / "mcp_jobs"
)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
MAX_CONCURRENT = int(os.getenv("TA_MCP_MAX_CONCURRENT", "2"))
BACKTEST_HARD_CAP = 500
ALL_ANALYSTS = ("market", "social", "news", "fundamentals")
Analyst = Literal["market", "social", "news", "fundamentals"]
_PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "openrouter": "OPENROUTER_API_KEY",
}
_RERUN_HINT = {
    "analysis": "Run run_pipeline again with the same ticker, date and settings; "
                "it resumes from the last finished step.",
    "backtest": "Run run_backtest again with this run_id; finished cells are skipped.",
    "settle": "Run pipeline_settle again.",
}

_lock = threading.Lock()
_slots = threading.Semaphore(MAX_CONCURRENT)
_jobs: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------- sleep guard

class _Awake:
    """Keep the Mac from sleeping while any job is queued or running.

    A job outlives Claude's turn, and the app's own keep-awake covers only turns,
    so an idle Mac could sleep mid-run. macOS `caffeinate -is` blocks idle sleep
    (and system sleep on power); `-w <pid>` ends it if this server dies. A closed
    lid still sleeps. Set TA_MCP_KEEP_AWAKE=0 to turn it off.
    """

    def __init__(self) -> None:
        self.active = 0
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def enabled(self) -> bool:
        return os.getenv("TA_MCP_KEEP_AWAKE", "1") != "0" and shutil.which("caffeinate") is not None

    def hold(self, delta: int) -> None:
        with self.lock:
            self.active = max(0, self.active + delta)
            if self.active and self.proc is None and self.enabled():
                self.proc = subprocess.Popen(
                    ["caffeinate", "-is", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif not self.active and self.proc is not None:
                self.proc.terminate()
                self.proc = None

    @property
    def holding(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


_awake = _Awake()


# ---------------------------------------------------------------- job store

def _job_file(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _save(job: dict[str, Any]) -> None:
    tmp = _job_file(job["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
    tmp.replace(_job_file(job["id"]))


def _update(job_id: str, **fields: Any) -> dict[str, Any]:
    with _lock:
        job = _jobs[job_id]
        job.update(fields)
        _save(job)
        return dict(job)


def _load_jobs() -> None:
    """Load past jobs; any left 'queued'/'running' died with a previous process."""
    for f in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("status") in ("queued", "running"):
            job["status"] = "interrupted"
            job["error"] = ("The server stopped before this job finished (the Claude Code "
                            "session closed?). " + _RERUN_HINT.get(job.get("kind", "analysis"), ""))
            _save(job)
        _jobs[job["id"]] = job


def _get(job_id: str) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise ValueError(f"No job with id {job_id!r}. Use pipeline_jobs to see known jobs.")
        return dict(job)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _start(kind: str, fields: dict[str, Any], work: Callable[[str], dict[str, Any]]) -> str:
    """Record a queued job and run `work(job_id)` in a thread; its dict is merged on success."""
    job_id = uuid.uuid4().hex[:8]
    job = {"id": job_id, "kind": kind, "status": "queued", "created_at": _now(), **fields}
    with _lock:
        _jobs[job_id] = job
        _save(job)

    def runner() -> None:
        _awake.hold(+1)
        try:
            with _slots:
                _update(job_id, status="running", started_at=_now())
                t0 = time.time()
                try:
                    # stdout carries the MCP protocol in stdio mode; keep library prints off it.
                    with contextlib.redirect_stdout(sys.stderr):
                        result = work(job_id)
                    _update(job_id, status="done", current_stage=None,
                            elapsed_s=round(time.time() - t0), finished_at=_now(), **result)
                except Exception as exc:  # report every failure back to Claude
                    traceback.print_exc(file=sys.stderr)
                    _update(job_id, status="failed", current_stage=None,
                            error=f"{type(exc).__name__}: {exc}",
                            elapsed_s=round(time.time() - t0), finished_at=_now())
        finally:
            _awake.hold(-1)

    threading.Thread(target=runner, name=f"ta-{job_id}", daemon=True).start()
    return job_id


# ---------------------------------------------------------------- progress

class _Progress(BaseCallbackHandler):
    """Counts LLM calls and remembers the agent now working, for pipeline_status."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.llm_calls = 0

    def on_chat_model_start(self, serialized, messages, **kwargs):  # noqa: D401
        self.llm_calls += 1
        meta = kwargs.get("metadata") or {}
        # Inside an analyst subgraph the node is just "agent"; the checkpoint
        # namespace ("Market Analyst:<uuid>|agent") names the analyst.
        ns = meta.get("langgraph_checkpoint_ns") or meta.get("checkpoint_ns") or ""
        node = ns.split(":", 1)[0] if ns else meta.get("langgraph_node")
        with _lock:
            job = _jobs[self.job_id]
            job["llm_calls"] = self.llm_calls
            if node:
                job["current_stage"] = node


# ---------------------------------------------------------------- config

def _config(debate_rounds: int | None = None, risk_rounds: int | None = None,
            quick_model: str | None = None, deep_model: str | None = None,
            output_language: str | None = None) -> dict:
    """TradingAgents' config from ~/.tradingagents/.env, with this request's overrides."""
    from tradingagents.default_config import build_default_config

    cfg = build_default_config()
    for name, value in (("debate_rounds", debate_rounds), ("risk_rounds", risk_rounds)):
        if value is not None and not 1 <= value <= 3:
            raise ValueError(f"{name} must be 1, 2 or 3")
    if debate_rounds is not None:
        cfg["max_debate_rounds"] = debate_rounds
    if risk_rounds is not None:
        cfg["max_risk_discuss_rounds"] = risk_rounds
    if quick_model:
        cfg["quick_think_llm"] = quick_model.strip()
    if deep_model:
        cfg["deep_think_llm"] = deep_model.strip()
    if output_language:
        cfg["output_language"] = output_language.strip()
    return cfg


def _check_keys(cfg: dict | None = None) -> None:
    """Fail at once, with a fix, when a tier's provider has no API key."""
    if cfg is None:
        from tradingagents.default_config import build_default_config
        cfg = build_default_config()
    for tier in ("quick_think_provider", "deep_think_provider"):
        provider = (cfg.get(tier) or cfg.get("llm_provider") or "").lower()
        var = _PROVIDER_KEYS.get(provider)
        if var and not os.getenv(var, "").strip():
            raise ValueError(
                f"{var} is not set, and TradingAgents is configured to use {provider}. "
                "Add it to ~/.tradingagents/.env and restart Claude Code."
            )


def _settings(cfg: dict) -> dict:
    return {"provider": cfg.get("llm_provider"), "quick_model": cfg.get("quick_think_llm"),
            "deep_model": cfg.get("deep_think_llm"), "debate_rounds": cfg.get("max_debate_rounds"),
            "risk_rounds": cfg.get("max_risk_discuss_rounds"), "language": cfg.get("output_language")}


def _ticker(raw: str) -> str:
    from tradingagents.dataflows.symbols import normalize_symbol

    t = normalize_symbol((raw or "").strip())
    if not t:
        raise ValueError("ticker is required")
    return t


def _asset_type(ticker: str) -> str:
    from tradingagents.dataflows.symbols import crypto_base

    return "crypto" if crypto_base(ticker) else "stock"


def _portfolio(portfolio: dict | None):
    if portfolio is None:
        return None
    from tradingagents.portfolio import PortfolioContext

    return PortfolioContext.model_validate(portfolio)


# ---------------------------------------------------------------- tools: analysis

def run_pipeline(
    ticker: str,
    trade_date: str | None = None,
    analysts: list[Analyst] | None = None,
    portfolio: dict | None = None,
    debate_rounds: int | None = None,
    risk_rounds: int | None = None,
    quick_model: str | None = None,
    deep_model: str | None = None,
    output_language: str | None = None,
    restart: bool = False,
) -> dict:
    """Start the full TradingAgents LangGraph pipeline (billed to the API key) for one
    ticker. Returns a job id at once; check it with pipeline_status, then read it with
    pipeline_report. Research output, not financial advice; it never places trades.

    A run takes 3-10 minutes: analysts, bull/bear debate, research manager, trader,
    risk debate, portfolio manager. Each finished step is checkpointed, so if a run
    fails or the session closes, calling this again with the same ticker, date and
    settings resumes where it stopped.

    Args:
        ticker: Yahoo Finance symbol with exchange suffix, e.g. NVDA, RELIANCE.NS,
            0700.HK, BTC-USD.
        trade_date: Analysis date YYYY-MM-DD. Defaults to today (fullest data).
        analysts: Subset of market, social, news, fundamentals. Default all four;
            fewer is cheaper and faster.
        portfolio: Current holdings so the trader and portfolio manager reason about
            the actual book, e.g. {"cash": 25000, "currency": "USD", "positions":
            [{"ticker": "NVDA", "quantity": 120, "average_price": 150.0}]}.
            An empty positions list means a flat book.
        debate_rounds: Bull/bear rounds, 1-3 (default from settings, usually 1).
            Each extra round adds two researcher calls.
        risk_rounds: Risk-team rounds, 1-3 (default usually 1).
        quick_model: Override the model for analysts, researchers, trader and risk
            team, e.g. claude-sonnet-5.
        deep_model: Override the model for the research and portfolio managers,
            e.g. claude-opus-5-5.
        output_language: Language for the reports, e.g. English, Hindi.
        restart: Discard a saved checkpoint for this ticker/date and start over.
    """
    t = _ticker(ticker)
    trade_date = trade_date or date.today().isoformat()
    date.fromisoformat(trade_date)
    chosen = list(dict.fromkeys(analysts or ALL_ANALYSTS))
    cfg = _config(debate_rounds, risk_rounds, quick_model, deep_model, output_language)
    cfg["checkpoint_enabled"] = True
    _check_keys(cfg)
    book = _portfolio(portfolio)  # validate before queueing
    asset_type = _asset_type(t)

    def work(job_id: str) -> dict:
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        progress = _Progress(job_id)
        ta = TradingAgentsGraph(selected_analysts=tuple(chosen), config=cfg, callbacks=[progress])
        if restart:
            ta.clear_checkpoint_on_success(t, trade_date, asset_type, book)
        state, signal = ta.propagate(t, trade_date, asset_type=asset_type, portfolio=book)
        report_md = ta.save_reports(state, t)
        return {"signal": signal, "decision": state.get("final_trade_decision", ""),
                "report_md": str(report_md), "report_html": str(report_md.with_suffix(".html")),
                "report_id": report_md.parent.name, "llm_calls": progress.llm_calls}

    job_id = _start("analysis", {"ticker": t, "trade_date": trade_date, "analysts": chosen,
                                 "portfolio": portfolio, "settings": _settings(cfg),
                                 "llm_calls": 0}, work)
    return {"job_id": job_id, "status": "queued", "settings": _settings(cfg),
            "note": "Runs usually take 3-10 minutes. Call pipeline_wait with this job_id to wait for the result."}


# ---------------------------------------------------------------- tools: backtest

def run_backtest(
    tickers: list[str],
    start_date: str,
    end_date: str,
    every_n_days: int = 7,
    analysts: list[Analyst] | None = None,
    portfolio: dict | None = None,
    debate_rounds: int | None = None,
    risk_rounds: int | None = None,
    quick_model: str | None = None,
    deep_model: str | None = None,
    max_cells: int = 12,
    run_id: str | None = None,
) -> dict:
    """Backtest the pipeline: run it for every ticker on every date of a grid, then
    score the decisions by rating (how often each rating called the direction, and its
    mean alpha against the regional benchmark). Returns a job id at once.

    COST: every cell is a full pipeline run billed to the API key, so tickers x dates
    multiplies the bill. The call is refused when the grid has more than max_cells
    cells; tell the user the cell count and get their go-ahead before raising it.

    Runs use a memory log of their own, never the live one. Data the vendors cannot
    date (fundamentals profile, insider trades, StockTwits, prediction markets, most
    news) is withheld for past dates, so backtests lean on prices, filings and macro.
    Cells whose holding window has not traded yet stay pending; run again later with
    the same run_id to settle and rescore them (finished cells are skipped).

    Args:
        tickers: Symbols, all stocks or all crypto (run crypto separately).
        start_date / end_date: Grid bounds YYYY-MM-DD; dates past today are dropped.
        every_n_days: Spacing of analysis dates (default weekly).
        analysts: As in run_pipeline. Fewer analysts make each cell cheaper.
        portfolio: One standing book for every cell (not carried forward).
        debate_rounds / risk_rounds / quick_model / deep_model: As in run_pipeline.
        max_cells: Cost guard, default 12, hard cap 500.
        run_id: Continue or rescore an earlier backtest.
    """
    from tradingagents.backtest import iter_grid

    syms = list(dict.fromkeys(_ticker(t) for t in tickers))
    if not syms:
        raise ValueError("tickers is required")
    kinds = {_asset_type(s) for s in syms}
    if len(kinds) > 1:
        raise ValueError("Mixes stocks and crypto; backtest them separately.")
    dates = iter_grid(start_date, end_date, every_n_days)
    cells = len(syms) * len(dates)
    if cells == 0:
        raise ValueError("The grid has no dates on or before today.")
    if not 1 <= max_cells <= BACKTEST_HARD_CAP:
        raise ValueError(f"max_cells must be between 1 and {BACKTEST_HARD_CAP}")
    if cells > max_cells:
        raise ValueError(
            f"This grid is {cells} cells ({len(syms)} tickers x {len(dates)} dates), each a "
            f"full pipeline run billed to the API key, over the max_cells guard of {max_cells}. "
            "Confirm the cost with the user, then call again with max_cells raised, or use "
            "fewer tickers, a shorter range or a larger every_n_days.")
    chosen = list(dict.fromkeys(analysts or ALL_ANALYSTS))
    cfg = _config(debate_rounds, risk_rounds, quick_model, deep_model)
    _check_keys(cfg)
    book = _portfolio(portfolio)
    asset_type = kinds.pop()

    def work(job_id: str) -> dict:
        from tradingagents.backtest import run_backtest as ta_backtest, summarize

        def progress(done: int, total: int, ticker: str, day: str) -> None:
            with _lock:
                _jobs[job_id].update(cells_done=done - 1, cells_todo=total,
                                     current_stage=f"{ticker} {day} (cell {done}/{total})")

        result = ta_backtest(syms, dates, cfg, asset_type=asset_type, portfolio=book,
                             selected_analysts=tuple(chosen), run_id=run_id, progress=progress)
        try:
            summary = summarize(result).render()
        except FileNotFoundError:
            summary = "No cell finished, so there is nothing to score."
        return {"run_id": result.run_id, "cells_run": result.cells_run,
                "cells_skipped": result.skipped, "cells_done": result.cells_run,
                "failures": [f"{t} {d}: {e}" for t, d, e in result.failures],
                "settlement_failures": [f"{t}: {e}" for t, e in result.settlement_failures],
                "summary": summary, "log_path": str(result.log_path)}

    job_id = _start("backtest", {"tickers": syms, "dates": dates, "analysts": chosen,
                                 "cells": cells, "run_id": run_id, "settings": _settings(cfg)}, work)
    return {"job_id": job_id, "status": "queued", "cells": cells, "dates": dates,
            "settings": _settings(cfg),
            "note": f"{cells} pipeline runs at roughly 3-10 minutes each. "
                    "Call pipeline_status with this job_id to follow it."}


# ---------------------------------------------------------------- tools: settle

def pipeline_settle() -> dict:
    """Score every logged decision whose holding window has now traded, with
    TradingAgents' own reflection written by the API model, into the live memory log.
    The portfolio manager reads these lessons on later runs. A run already settles
    the decisions it finds; use this for tickers you have stopped analysing, or on a
    schedule. Returns a job id; results come from pipeline_report."""
    cfg = _config()
    _check_keys(cfg)

    def work(job_id: str) -> dict:
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        _update(job_id, current_stage="settling")
        result = TradingAgentsGraph(config=cfg).settle_all_pending()
        return {"settled": [f"{t} {d}" for t, d in result.settled],
                "settle_failures": [f"{t} {d}: {why}" for t, d, why in result.failed]}

    job_id = _start("settle", {}, work)
    return {"job_id": job_id, "status": "queued",
            "note": "Usually under a minute. Call pipeline_report with this job_id."}


# ---------------------------------------------------------------- tools: status

_PUBLIC = ("id", "kind", "ticker", "trade_date", "tickers", "cells", "cells_done", "cells_todo",
           "run_id", "analysts", "settings", "status", "signal", "current_stage", "llm_calls",
           "created_at", "started_at", "finished_at", "error")


def _public(job: dict[str, Any]) -> dict[str, Any]:
    out = {k: job[k] for k in _PUBLIC if job.get(k) is not None}
    out.setdefault("kind", "analysis")  # jobs saved before kinds existed
    if job.get("status") == "running" and job.get("started_at"):
        started = datetime.fromisoformat(job["started_at"])
        out["elapsed_s"] = round((datetime.now() - started).total_seconds())
    elif job.get("elapsed_s") is not None:
        out["elapsed_s"] = job["elapsed_s"]
    if job.get("status") in ("queued", "running"):
        out["keeping_mac_awake"] = _awake.holding
    return out


def pipeline_status(job_id: str) -> dict:
    """Check a pipeline job (analysis, backtest or settle): queued, running (with the
    agent or backtest cell now working), done, failed or interrupted."""
    return _public(_get(job_id))


def pipeline_report(job_id: str, detail: Literal["summary", "full"] = "summary") -> dict:
    """Read a finished pipeline job.

    analysis: the 5-tier rating (Buy / Overweight / Hold / Underweight / Sell, or
    REVIEW) and the portfolio manager's decision; detail="full" adds the complete
    report (every analyst, the bull/bear debate, trader plan, risk debate).
    backtest: the scorecard by rating, cells run, failures and the run_id.
    settle: which decisions were settled and which failed.
    """
    job = _get(job_id)
    if job["status"] != "done":
        return _public(job)
    kind = job.get("kind", "analysis")
    if kind == "backtest":
        keys = ("run_id", "tickers", "dates", "cells_run", "cells_skipped", "summary",
                "failures", "settlement_failures", "settings")
        return {"kind": kind, **{k: job.get(k) for k in keys}}
    if kind == "settle":
        return {"kind": kind, "settled": job.get("settled", []),
                "failures": job.get("settle_failures", [])}
    out = {"kind": kind, "ticker": job["ticker"], "trade_date": job["trade_date"],
           "signal": job["signal"], "decision": job.get("decision", ""),
           "report_id": job.get("report_id"), "report_html": job.get("report_html"),
           "settings": job.get("settings")}
    if detail == "full":
        text = Path(job["report_md"]).read_text(encoding="utf-8")
        limit = 80_000
        out["full_report"] = text if len(text) <= limit else (
            text[:limit] + f"\n\n[truncated; full report at {job['report_md']}]")
    return out


async def pipeline_wait(job_id: str, timeout_s: int = 240, ctx: Context | None = None) -> dict:
    """Wait for a pipeline job to finish, then return its report (as pipeline_report
    with detail="summary"). Returns early with the current status if it is still
    running after timeout_s (10-600 seconds); call again to keep waiting.

    Prefer this over repeated pipeline_status calls when the user is waiting: the turn
    ends with the result, so Remote Control notifies their phone when it is ready.
    """
    if not 10 <= timeout_s <= 600:
        raise ValueError("timeout_s must be between 10 and 600")
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    while True:
        job = _get(job_id)
        if job["status"] not in ("queued", "running"):
            break
        if time.monotonic() >= deadline:
            return {**_public(job), "note": "Still running; call pipeline_wait again to keep waiting."}
        if ctx is not None:
            with contextlib.suppress(Exception):  # progress is a courtesy; never fail the wait on it
                await ctx.report_progress(round(time.monotonic() - started), None,
                                          job.get("current_stage") or job["status"])
        await anyio.sleep(3)
    return pipeline_report(job_id) if job["status"] == "done" else _public(job)


def pipeline_jobs(limit: int = 20, kind: Literal["analysis", "backtest", "settle"] | None = None) -> list[dict]:
    """List recent pipeline jobs, newest first, optionally of one kind."""
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j.get("created_at", ""), reverse=True)
        if kind:
            jobs = [j for j in jobs if j.get("kind", "analysis") == kind]
        return [_public(j) for j in jobs[:limit]]


def keys_present() -> bool:
    try:
        _check_keys()
        return True
    except ValueError:
        return False


TOOLS = (run_pipeline, run_backtest, pipeline_settle, pipeline_wait, pipeline_status,
         pipeline_report, pipeline_jobs)


def register(mcp) -> None:
    _load_jobs()
    for fn in TOOLS:
        mcp.tool()(fn)

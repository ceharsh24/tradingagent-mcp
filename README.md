# TradingAgents MCP

A local MCP server and research skill for Codex and Claude Code, built on the upstream [TradingAgents](https://github.com/TauricResearch/TradingAgents) Python library. Use it to research stocks and crypto, check options strategies, and save reports and decision history.

`uv` installs the pinned TradingAgents dependency in the project's `.venv`. Codex launches `server.py` as a local MCP server over standard input and output. The server imports TradingAgents and exposes selected capabilities as MCP tools, including market data, options calculations, and report storage. The `trading-analysis` skill guides the client through the research workflow. The original TradingAgents pipeline is also available with [separate model sign-in](#optional-original-tradingagents-pipeline).

> For research. This is an independent project. See [NOTICE](NOTICE) for upstream attribution.

## Install

### Codex

Install the [Codex CLI](https://learn.chatgpt.com/docs/cli), sign in with your own account, and install [uv](https://docs.astral.sh/uv/). Clone this repository and run the following from its root on macOS or Linux. If you already have a `trading-analysis` skill link, check that it points to this checkout and skip the `ln -s` command if it does.

```bash
uv sync --frozen --no-dev
codex mcp add tradingagents \
  --env "TRADINGAGENTS_ENV_FILE=$PWD/.env.codex" \
  --env TRADINGAGENTS_FRONTEND=Codex \
  -- "$(command -v uv)" run --directory "$PWD" --frozen --no-dev --quiet python server.py

mkdir -p "$HOME/.codex/skills"
ln -s "$PWD/skills/trading-analysis" "$HOME/.codex/skills/trading-analysis"
codex mcp list
```

Open the checkout in a fresh Codex chat and ask it to use TradingAgents to fetch an NVDA market snapshot. Your existing Codex sign-in is enough for this workflow.

#### Optional original TradingAgents pipeline

To run the upstream pipeline with an eligible ChatGPT Plus or Pro plan, create `.env.codex` from the provided example and complete the separate TradingAgents sign-in. Usage counts against your plan.

```bash
test -f .env.codex || cp .env.codex.example .env.codex
.venv/bin/tradingagents login chatgpt
```

In `~/.codex/config.toml`, add `tool_timeout_sec = 660` to the existing `[mcp_servers.tradingagents]` section so `pipeline_wait` has time to finish. To require confirmation for backtests, add the following at the end of the file. Open a fresh Codex chat after setup.

```toml
[mcp_servers.tradingagents.tools.run_backtest]
approval_mode = "prompt"
```

#### Optional read-only Robinhood connection

Robinhood hosts a separate MCP server. To enable the documented read-only tools for external agents, add this entry to `~/.codex/config.toml`, or update the existing `robinhood` entry. Trim the list to the tools you want Codex to access.

```toml
[mcp_servers.robinhood]
url = "https://agent.robinhood.com/mcp/trading"
enabled_tools = [
    # Account and portfolio
    "get_accounts",
    "get_portfolio",
    "get_realized_pnl",
    "get_pnl_trade_history",
    "search",
    "get_limited_margin_upgrade_info",
    "get_trade_approvals",
    "get_trade_approval_setting",
    # Watchlists
    "get_watchlists",
    "get_watchlist_items",
    "get_option_watchlist",
    "get_popular_watchlists",
    # Market data
    "get_equity_historicals",
    "get_equity_fundamentals",
    "get_financials",
    "get_equity_price_book",
    "get_equity_technical_indicators",
    "get_earnings_results",
    "get_earnings_calendar",
    "get_indexes",
    "get_index_quotes",
    "get_sec_filing_index",
    "get_sec_filing",
    "get_sec_filing_facts",
    "get_sec_filing_facts_catalog",
    "get_index_historicals",
    "get_politician_trades",
    "get_equity_analyst_ratings",
    # Equities
    "get_equity_positions",
    "get_equity_tax_lots",
    "get_equity_quotes",
    "get_equity_orders",
    "get_equity_tradability",
    # Options
    "get_option_level_upgrade_info",
    "get_option_chains",
    "get_option_historicals",
    "get_option_instruments",
    "get_option_quotes",
    "get_option_positions",
    "get_option_orders",
    # Crypto
    "get_currency_pairs",
    "get_crypto_quotes",
    "get_crypto_positions",
    "get_crypto_orders",
    "get_crypto_account_onboarding_info",
    "get_crypto_tax_lots",
    # Scanners
    "get_scans",
    "get_scanner_filter_specs",
    "run_scan",
    "get_scanner_datapoints",
    "preview_scan",
    # Alerts
    "get_alerts",
    "get_alert_log",
    # Advanced order history
    "get_advanced_orders",
    # Legend
    "get_legend_charts",
    "get_legend_indicators",
    "get_legend_indicator_catalog",
    "get_legend_layouts",
]
```

Run the login command, authorize your own Robinhood account in the browser, then open a fresh Codex chat. See [Robinhood's onboarding instructions](https://robinhood.com/us/en/support/articles/agentic-trading-overview/).

```bash
codex mcp login robinhood
```

Account-specific reads use a brokerage account number. With `get_accounts` enabled, Codex can look up your linked accounts. The allowlist includes portfolio and position reads, order history, market data, watchlists, scanner results, alerts, and Legend data. Actual availability depends on the tools Robinhood advertises to your signed-in session. Trade submission, cancellation, exercises, order previews, transfers, and changes to saved settings are outside this allowlist. See [Robinhood's external-agent tool reference](https://robinhood.com/us/en/support/articles/trading-with-your-agent/#Agenttools).

### Claude Code

Install [Claude Code](https://claude.com/claude-code) and [uv](https://docs.astral.sh/uv/). In Claude Code, add the local checkout as a marketplace and install the plugin using its existing marketplace name.

```text
/plugin marketplace add /path/to/tradingagents-mcp
/plugin install tradingagents@tradingagents-claude
```

Restart Claude Code. On the first start, the plugin downloads TradingAgents and its Python dependencies, which takes about a minute. If its tools are missing in that first session, run `/mcp` and reconnect `tradingagents`.

## Use

> Analyse NVDA
>
> Run trading analysis on RELIANCE.NS, quick depth. I hold 50 shares at ₹2,400.
>
> Settle my past trading decisions
>
> What's NVDA's implied move into earnings? Price a covered call for my 200 shares.

Depth options

- `quick`: about 5 subagents. The main agent writes the debate, risk views, and a short fact check.
- `standard` (the default): about 13 subagents
- `deep`: adds a second debate round

### Options

These work for listed options, mostly US, using Yahoo's live chains, so today only. Yahoo has no NSE/BSE options. Every figure is computed in code:

- `options_overview`: IV term structure, implied move per expiry, 25-delta skew, put/call open interest, max pain, realized volatility, next earnings date
- `options_chain`: one expiry's near-the-money strikes, with Black-Scholes deltas and the largest open-interest strikes
- `options_strategy`: prices a position from the live quotes: net debit/credit, max profit/loss, breakevens, probability of profit, net delta

The Options Strategist proposes defined-risk structures only and prices each one with `options_strategy`.

### Fact check

Before the portfolio manager decides, a Fact Checker re-fetches the 15–30 figures the decision rests on and marks each one verified, wrong (with the correct value) or unsupported. The portfolio manager ignores anything wrong or unsupported. The check is saved as its own report section.

From your phone: start a Claude Code session on your computer, turn on Remote Control, and open the session in the Claude mobile app.

## Two engines

| | Codex or Claude Code runs the agents (default) | Original TradingAgents pipeline |
|---|---|---|
| How | The `trading-analysis` skill guides analysts, a bull/bear debate, a risk review, and a fact check using the local MCP tools. | `run_pipeline` runs TradingAgents' own LangGraph workflow. |
| Usage | Your Codex or Claude Code session | Your ChatGPT plan or configured model-provider API key |
| Setup | Register the MCP server and skill using the instructions above. | Follow the [ChatGPT sign-in steps](#optional-original-tradingagents-pipeline) for Codex, or configure the provider's API key, such as `ANTHROPIC_API_KEY` for Claude Code. |

### Pipeline commands

Ask in plain words:

> Run the pipeline on NVDA with 2 debate rounds
>
> Backtest NVDA and AAPL weekly from June to September
>
> Settle my past decisions

| Tool | What it does |
|---|---|
| `run_pipeline` | One analysis. You can set analysts, portfolio, debate and risk rounds, quick and deep models, and report language per run. Every step is checkpointed, so a failed or interrupted run resumes when you ask again (`restart=true` starts over). |
| `run_backtest` | Runs the pipeline for every ticker on every date of a grid and scores each rating: how often it called the direction, and its mean alpha against the benchmark. |
| `pipeline_settle` | Scores due decisions in the live memory log, using TradingAgents' own reflections |
| `pipeline_wait` | Waits for the job in the current turn and returns the report when it finishes |
| `pipeline_status`, `pipeline_report`, `pipeline_jobs` | Follow and read jobs |

Each pipeline job takes about 3–10 minutes. After a pipeline run, the skill uses `annotate_report` to add fact check and options sections.

Jobs run inside the MCP server, so keep the Codex or Claude Code session open until a job finishes. If you close it, ask again and the run resumes from its checkpoint. While a job runs, the server keeps your Mac awake with `caffeinate`. A closed lid still sleeps. Set `TA_MCP_KEEP_AWAKE=0` to turn this off.

Backtests cost one full pipeline run per cell (tickers × dates). They are refused above `max_cells` (default 12), so you have to confirm a larger grid. They use a memory log of their own, never your live one.

### Either engine

- `list_reports` and `read_report`: past analyses from both engines
- `annotate_report`: adds or replaces the Options and Fact Check sections of a saved report
- `backtest_results`: past backtest scorecards, read at no cost

## Configure (optional)

Copy [.env.example](.env.example) to `.env` in this folder. It's gitignored, so keys never get committed. Settings load in this order, and the first file to set a value wins:

1. The file named by `TRADINGAGENTS_ENV_FILE` (Codex uses `.env.codex`)
2. `<repo>/.env`
3. `~/.tradingagents/.env` (use this for a plugin installed from GitHub, whose folder is replaced on update)

Useful settings:

- `SEC_EDGAR_USER_AGENT`: your name and email. SEC asks callers to identify themselves.
- `FRED_API_KEY`: free; adds US macro data (rates, CPI, yields).
- `ANTHROPIC_API_KEY` plus the model settings: turns on the full pipeline.

## Where things are saved

- Reports: `~/.tradingagents/logs/reports/<TICKER>_<time>/complete_report.{md,html}`
- Decision memory: `~/.tradingagents/memory/trading_memory.md`
- Pipeline jobs: `~/.tradingagents/mcp_jobs/`

## Data

Market data comes from TradingAgents' sources:

- Yahoo Finance
- SEC EDGAR (US filings as originally filed)
- StockTwits and Reddit
- FRED
- Polymarket

Every dated tool takes an analysis date and never returns data published after it.

Some sources only have live data: the fundamentals profile, insider trades, StockTwits, Polymarket, most news and all options data. These are withheld for past dates, so analyses dated today have the fullest data.

Stocks work on any exchange Yahoo covers (`RELIANCE.NS`, `0700.HK`, `AZN.L`, …), and crypto works as `BTC-USD`.

## Develop

From the cloned repository root, install the development dependencies and run the checks.

```bash
uv sync --frozen
uv run pytest
uv run python tests/smoke_stdio.py
```

| File | What it is |
|---|---|
| `server.py` | The MCP server: data, memory and report tools |
| `pipeline.py` | Optional tools for the full LangGraph pipeline |
| `options.py` | Options data from Yahoo, plus Black-Scholes and payoff math |
| `skills/trading-analysis/SKILL.md` | The analysis workflow |
| `hooks/ensure-env.sh` | Prepares the Python environment when a session starts |
| `.claude-plugin/plugin.json` | Plugin manifest |
| `.claude-plugin/marketplace.json` | Marketplace manifest |

TradingAgents is pinned to v0.7.1 in `pyproject.toml` and `uv.lock`. To upgrade, change the tag, run `uv lock`, run the tests, then bump the version in both manifests.

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

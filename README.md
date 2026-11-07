# tradingagents-claude

A Claude Code plugin for multi-agent stock analysis in the style of [TradingAgents](https://github.com/TauricResearch/TradingAgents).

Ask Claude to analyse a ticker. A team of agents works through it: market, sentiment, news, fundamentals and options analysts, a bull/bear debate, a trader, an options strategist, a risk team, a fact checker and a portfolio manager. The result is a Buy / Overweight / Hold / Underweight / Sell rating and a saved HTML report. Each decision is logged and later scored against what the market actually did.

> **Research only, not financial advice.** Nothing here places trades.
>
> Not affiliated with Tauric Research. See [NOTICE](NOTICE).

## Install

You need [Claude Code](https://claude.com/claude-code) and [uv](https://docs.astral.sh/uv/).

From a local folder (it then runs straight from that folder):

```
/plugin marketplace add /path/to/tradingagents-claude
/plugin install tradingagents@tradingagents-claude
```

Or, once it's published on GitHub: `/plugin marketplace add ceharsh24/tradingagents-claude`, then the same install command.

Restart Claude Code. On the first start, the plugin downloads TradingAgents and its Python dependencies, which takes about a minute. If its tools are missing in that first session, run `/mcp` and reconnect `tradingagents`.

## Use

> Analyse NVDA
>
> Run trading analysis on RELIANCE.NS, quick depth. I hold 50 shares at ₹2,400.
>
> Settle my past trading decisions
>
> What's NVDA's implied move into earnings? Price a covered call for my 200 shares.

Depth options:

- `quick`: about 5 subagents; Claude writes the debate, the risk views and a short fact check itself
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

| | Claude runs the agents (default) | Original TradingAgents pipeline |
|---|---|---|
| How | The `trading-analysis` skill: Claude and its subagents play each role, using the plugin's data tools | The `run_pipeline` tools run TradingAgents' own LangGraph pipeline |
| Billed to | Your Claude plan | Your API key |
| Turn on | Works out of the box | Set `ANTHROPIC_API_KEY` in `~/.tradingagents/.env`, then restart Claude Code |

### With an API key

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
| `pipeline_wait` | Waits in the same turn until a job finishes and returns its report, so Remote Control notifies your phone |
| `pipeline_status`, `pipeline_report`, `pipeline_jobs` | Follow and read jobs |

Each pipeline job takes 3–10 minutes. After a pipeline run, Claude adds the fact check and options sections on your plan, using `annotate_report`.

Jobs run inside the MCP server, so keep the Claude Code session open until a job finishes. If you close it, ask again and the run resumes from its checkpoint. While a job runs, the server keeps your Mac awake with `caffeinate`. A closed lid still sleeps. Set `TA_MCP_KEEP_AWAKE=0` to turn this off.

Backtests cost one full pipeline run per cell (tickers × dates). They are refused above `max_cells` (default 12), so you have to confirm a larger grid. They use a memory log of their own, never your live one.

### Either engine

- `list_reports` and `read_report`: past analyses from both engines
- `annotate_report`: adds or replaces the Options and Fact Check sections of a saved report
- `backtest_results`: past backtest scorecards, read at no cost

## Configure (optional)

Copy [.env.example](.env.example) to `~/.tradingagents/.env`. That folder survives plugin updates. Useful settings:

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

```bash
git clone https://github.com/ceharsh24/tradingagents-claude && cd tradingagents-claude
uv sync
uv run pytest
uv run python tests/smoke_stdio.py
```

To run your checkout as the plugin, run `/plugin marketplace add /path/to/tradingagents-claude` and install it from there. The plugin then runs straight from your folder.

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

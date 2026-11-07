---
name: trading-analysis
description: Run a TradingAgents-style multi-agent analysis of a stock or crypto ticker (analyst team including an options analyst, bull/bear debate, trader, options strategist, risk team, fact checker, portfolio manager) using the tradingagents MCP tools, ending in a Buy/Overweight/Hold/Underweight/Sell rating and a saved report; or run the original TradingAgents pipeline or a backtest when an API key is configured. Use when the user asks to analyse, research, rate, backtest or "run TradingAgents / the pipeline on" a ticker, asks about a ticker's options (implied move, IV, pricing a covered call or spread), or wants past reports or decisions reviewed.
---

# Trading analysis (TradingAgents, run by Claude)

You run the TradingAgents firm yourself: subagents play the analysts, debaters and
fact checker, and you play the Research Manager, Trader, Options Strategist and
Portfolio Manager. Market data, options math, memory and report saving come from the
`tradingagents` MCP tools. This is research, not financial advice, and nothing here
places trades. Say so once in the final answer.

## Choose the path first

- **Past work.** "What did we say about NVDA?" → `list_reports(ticker)`, then
  `read_report(report_id)`. "How did the backtest do?" → `backtest_results`.
- **Options question only** ("NVDA's implied move into earnings", "price a covered
  call on AAPL", "is IV rich?") → answer with `options_overview`, `options_chain` and
  `options_strategy` directly, using the Options Strategist rules below. No full analysis.
- **API pipeline**, only if the `run_pipeline` tool exists (an API key is set). Use it
  when the user says pipeline, API, "full TradingAgents", or asks for a backtest.
  - Analysis: `run_pipeline(ticker, trade_date, analysts, portfolio, debate_rounds,
    risk_rounds, …)`, then `pipeline_wait(job_id)` until it returns the report (call
    again if it says still running). Waiting in the turn is what makes Remote Control
    notify the user's phone when the result is ready. If a run failed or was
    interrupted, the same `run_pipeline` call resumes from the last finished step.
  - Then add what the pipeline lacks, on the Claude plan: read
    `pipeline_report(job_id, detail="full")`; run the **Fact Checker** (Step 8) on it
    and, when options apply, the **Options Analyst** brief (Step 2) and **Options
    Strategist** (Step 6); attach them with `annotate_report(report_id, …)`. If the
    fact check finds an error that matters, say so next to the pipeline's rating.
  - Backtest: `run_backtest(tickers, start_date, end_date, every_n_days)`. Each cell
    is a full paid run. Before raising `max_cells`, state the cell count and get a
    clear yes. Wait with `pipeline_wait`; later read `backtest_results(run_id)`, and
    rerun with the `run_id` to settle pending cells.
  - Settle: `pipeline_settle` writes TradingAgents' own reflections for due decisions.
- **Claude runs the agents** (default otherwise, no API cost): follow the steps below.

## Inputs

Read from the request; ask only if the ticker is missing.

- **ticker**: Yahoo symbol with exchange suffix (NVDA, RELIANCE.NS, TCS.NS, 0700.HK, BTC-USD).
  Map plain names to symbols yourself ("Reliance" → RELIANCE.NS) and say which you used.
- **as_of**: analysis date, default today. A past date gives a point-in-time view.
  Several sources (fundamentals profile, insider trades, StockTwits, prediction
  markets, most company news, all options data) only exist live and are withheld for
  past dates; the tools say so, and agents must not fill the gap from their own knowledge.
- **analysts**: default market, sentiment, news, fundamentals, plus **options** when
  as_of is today and `options_overview` finds listed options (mostly US; NSE has none
  on Yahoo). Skip options if the user says so; skip fundamentals for crypto.
- **depth**: `quick` (no subagents for debate, risk or fact check), `standard`
  (default), `deep` (two debate rounds).
- **portfolio** (optional): cash and positions the user mentions. Pass to the Trader,
  Options Strategist, risk team and Portfolio Manager. No portfolio means "no knowledge
  of holdings", not "flat".

## Step 0: settle the memory log

Call `unsettled_decisions`. For every item with status `ready`, write a 2–4 sentence
reflection (was the rating right given the raw return and alpha, why, and the lesson)
and call `settle_decision`. Skip `waiting` items. Mention settlements in one line at the end.

## Step 1: identify the instrument

Call `instrument_profile(ticker, as_of)`. Paste its text into every subagent prompt
below as INSTRUMENT. Use the exact ticker it returns everywhere.

## Step 2: analyst team, in parallel

Launch one subagent per analyst **in a single message** (Agent tool, general-purpose).
Each prompt contains: INSTRUMENT, `as_of`, the role brief below, and these rules:

> Use only the `tradingagents` MCP tools, always passing as_of=<as_of>. Do not use web
> search or your own knowledge of prices, news or results after the analysis date. If
> a tool says data is withheld or unavailable, say so in the report; never invent
> figures or do arithmetic the tools already did. Write a markdown report (500–900
> words) ending in a table of key points (signal, evidence, direction). Return only
> the report.

Role briefs:

- **Market Analyst**: call `market_snapshot` first and treat it as the source of truth
  for exact figures; then `price_history` (about 3 months) and `technical_indicators`
  with up to 8 complementary indicators chosen for this market (trend, momentum,
  volatility, volume; no redundant pairs). Cover trend, momentum, volatility, key
  support/resistance and ATR-based stop distances.
- **Sentiment Analyst**: call `social_sentiment` and `company_news`. Report the balance
  of bullish vs bearish chatter, what drives it, how reliable it is (sample size,
  bots, hype), and any gap between social mood and news.
- **News Analyst**: call `company_news`, `global_news`, and where relevant
  `macro_indicator` (rates, CPI, yields, VIX; for India and others choose what fits)
  and `prediction_markets`. Cover company events, sector and macro forces, and the
  events to watch.
- **Fundamentals Analyst**: call `fundamentals`, `financial_statement` for income
  statement, balance sheet and cashflow (quarterly; annual if useful), and
  `insider_transactions`. Cover growth, margins, cash generation, balance sheet,
  valuation versus growth, insider activity and red flags.
- **Options Analyst** (when options apply): call `options_overview`, then
  `options_chain` for the expiry that matters: the first one after earnings if earnings
  fall within 45 days, else the one nearest 30 days. Report what the options market
  prices, quoting the tools' figures: implied move and range versus the 20-day
  realized volatility; IV rich or cheap (ATM IV / realized); term structure and any
  earnings kink; skew (demand for downside protection); positioning (put/call ratios,
  largest open-interest strikes; treat max pain as weak evidence). End with what this
  means for entries, stops and targets.

Gather the reports. If one subagent failed, rerun it once, then go on without it and say so.

## Step 3: research debate

Give each side INSTRUMENT, all analyst reports and this rule: argue from the reports'
evidence, cite specific figures, and answer the other side's strongest points directly.

- `standard`: in parallel, a **Bull Researcher** subagent (the case for owning it:
  growth, edge, positive signals) and a **Bear Researcher** subagent (the case against:
  risks, weaknesses, negative signals), 300–500 words each. Then one rebuttal from each,
  in parallel, each given the other's opening.
- `deep`: as standard, with a second rebuttal round.
- `quick`: write both cases yourself, labelled.

## Step 4: Research Manager (you)

Judge the debate. Commit to the side the evidence supports; Hold only if it is truly
balanced. Output an **investment plan**: recommendation (Buy/Overweight/Hold/
Underweight/Sell), rationale, and strategic actions. 200–400 words.

## Step 5: Trader (you)

From the plan and the Market Analyst's levels, propose a transaction:
**Action** (Buy/Hold/Sell), reasoning (2–4 sentences), **entry price**, **stop-loss**
(absolute prices in the quote currency, grounded in `market_snapshot` and ATR, never
invented), and **position sizing** guidance; against the portfolio if given. Use the
options-implied range as a sanity check on stop and target distances.
End with the line `FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`.

## Step 6: Options Strategist (you, when options apply)

Turn the trader's direction, horizon and the IV picture into one to three structures.
Price **every** structure with `options_strategy`; never compute premiums, breakevens
or probabilities yourself. Rules:

- Match direction and horizon: bullish → bull call spread, cash-secured put, or a
  covered call if the user holds 100+ shares; bearish → bear put spread, or a collar
  on shares held; neutral or Hold → covered call, iron-condor-style credit spreads, or
  no trade.
- Match the IV picture: IV rich versus realized → favour selling premium (covered
  calls, cash-secured puts, credit spreads); IV cheap → favour debit spreads or long options.
- Defined risk only, unless the user explicitly asks otherwise. Never propose a
  naked short call.
- Respect the portfolio: covered calls need the shares; cash-secured puts need the cash.
- Say whether the expiry spans earnings, and what that does to the trade.

Output a table (structure, legs, net debit/credit, max profit, max loss, breakevens,
probability of profit, return on risk) plus one line each on when it fits and its main
risk. Pass this table to the risk team with the trader's proposal.

## Step 7: risk team

Three views of the trader's proposal (and the options structures, if any), each given
INSTRUMENT, the analyst reports, the plan, the proposal and the portfolio if any,
200–350 words each:

- **Aggressive**: the upside being left on the table; argue for more exposure.
- **Conservative**: what could go wrong; argue for protecting capital.
- **Neutral**: weigh both; argue for the balanced adjustment.

`standard`/`deep`: three subagents in parallel. `quick`: write all three yourself.

## Step 8: Fact Checker

Before the decision, verify the numbers everything rests on. `standard`/`deep`: one
subagent; `quick`: do it yourself on the 5–8 figures the decision depends on. Give it
INSTRUMENT, `as_of`, every report so far (analysts, debate, plan, trader, options
strategist, risk team) and this brief:

> Pick the 15–30 figures the decision depends on most: prices, levels, indicator
> values, growth and margin figures, valuation multiples, dates, IV and implied move,
> option prices and payoffs. Re-fetch each with the `tradingagents` tools
> (market_snapshot, technical_indicators, financial_statement, fundamentals,
> company_news, options_chain, options_strategy…), always with as_of=<as_of>. Mark each
> **verified** (matches within rounding), **wrong** (give the correct value and
> source), or **unsupported** (no tool can confirm it). Do not judge opinions, only
> facts. Output a markdown table (claim, where it appears, tool, status, correct
> value) and then "Corrections that matter", a short list of the wrong or unsupported
> claims that could change the decision, or "None".

## Step 9: Portfolio Manager (you)

Call `memory_lessons(ticker, as_of)` and use the lessons. Do not rely on any claim the
fact check marked wrong or unsupported; if a correction changes the picture, say how.
Then write the final decision. Rating scale (use exactly one):

- **Buy**: strong conviction to enter or add
- **Overweight**: favourable, increase exposure gradually
- **Hold**: maintain, no action
- **Underweight**: reduce exposure, take partial profits
- **Sell**: exit or avoid entry

Ground every conclusion in specific evidence. The risk debate always conflicts;
deciding which side is stronger is the job, so conflict alone is not a reason to Hold.
Commit to the stronger case, sized by how decisively it wins. Choose Hold only when the
evidence stays balanced after weighing, or is too thin. Write, in order:

```
**Rating**: <one of the five>
**Executive Summary**: the call and how to act on it (entry, stop, sizing, horizon; the preferred options structure if any)
**Investment Thesis**: the evidence that decided it, and what would change it
```

## Step 10: save

Call `save_analysis` with the ticker, as_of as trade_date, the rating, the Portfolio
Manager text as final_decision, and every report: market_report, sentiment_report,
news_report, fundamentals_report, bull_case and bear_case (opening plus rebuttals,
labelled by round), research_plan, trader_plan, aggressive_view, conservative_view,
neutral_view, options_analysis, options_strategy, fact_check. It logs the decision so
a later run can score it.

## Final answer (often read on a phone; keep it short)

1. `**<TICKER>: <Rating>**` with the as-of date
2. Executive summary, 2–3 sentences, with entry, stop and sizing
3. Options, if any: implied move to the key expiry, and the preferred structure with
   its max loss, breakeven and probability of profit, one or two lines
4. Strongest bull point and strongest bear point, one line each
5. What would change the call, one line
6. Fact check: "N figures checked, M corrected", naming any correction that mattered
7. Data gaps (withheld or unavailable sources), if any, one line
8. Where the report is saved (report_html), plus "Research only, not financial advice."

Then offer to go deeper on any agent's report, or to rerun with a different depth,
date or portfolio.

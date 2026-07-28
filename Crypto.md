
├── .env.example
├── config.py
├── core
│   ├── __init__.py
│   ├── database.py
│   ├── database_sync.py
│   ├── exchange.py
│   ├── executor.py
│   ├── instrument_cache.py
│   ├── logger.py
│   ├── notifier.py
│   ├── paper_trading.py
│   ├── position_manager.py
│   ├── risk_manager.py
│   ├── strategy_journal.py
│   └── tp_manager.py
├── data
│   └── runtime SQLite/JSONL files (ignored by git)
├── engine
│   ├── __init__.py
│   ├── filters.py
│   ├── indicators.py
│   ├── liquidity.py
│   ├── market_regime.py
│   ├── scoring.py
│   ├── smc
│   │   ├── __init__.py
│   │   ├── analyzer.py
│   │   ├── liquidity_engine.py
│   │   ├── poi_engine.py
│   │   ├── smc_utils.py
│   │   └── structure_engine.py
│   ├── smc_analyzer.py
│   ├── stats_analyzer.py
│   └── trend_engine.py
├── logs
│   └── runtime logs (ignored by git)
├── main.py
├── requirements-lock.txt
├── requirements.txt
├── scripts
│   ├── pre_commit_checks.py
│   ├── run_bybit_demo_lifecycle.py
│   ├── run_bybit_demo_lifecycle_soak.py
│   ├── run_market_regime_observer.py
│   ├── run_paper_lifecycle.py
│   ├── run_strategy_observer.py
│   ├── secret_scan.py
│   └── summarize_strategy_journal.py
├── tests
│   ├── test_bybit_demo_lifecycle.py
│   ├── test_bybit_demo_soak.py
│   ├── test_execution_safety.py
│   ├── test_market_regime_observer.py
│   ├── test_paper_lifecycle.py
│   ├── test_pre_commit_checks.py
│   ├── test_secret_scan.py
│   ├── test_strategy_journal.py
│   └── test_strategy_observer.py

Safe lifecycle checks:

```bash
python3 scripts/run_paper_lifecycle.py --db /tmp/cryptobot_paper_lifecycle.db --reset-db
python3 scripts/run_paper_lifecycle.py --db /tmp/cryptobot_paper_partial_lifecycle.db --reset-db --partial-fill-recovery
.venv/bin/python scripts/run_bybit_demo_lifecycle.py --symbol XRPUSDT --max-notional 15 --wait 20
.venv/bin/python scripts/run_bybit_demo_lifecycle_soak.py --iterations 3 --symbol XRPUSDT --max-notional 25 --wait 20 --sleep 3
.venv/bin/python scripts/run_strategy_observer.py --cycles 3 --sleep 60 --max-symbols 5
.venv/bin/python scripts/run_strategy_observer.py --cycles 10 --sleep 60 --max-symbols 0 --summary-only
.venv/bin/python scripts/run_market_regime_observer.py --cycles 10 --sleep 60 --max-symbols 0 --summary-only
.venv/bin/python scripts/run_market_regime_observer.py --cycles 288 --sleep 300 --max-symbols 0 --summary-only --progress-jsonl /tmp/cryptobot_regime_progress.jsonl --checkpoint-output /tmp/cryptobot_regime_checkpoint.json --final-output /tmp/cryptobot_regime_final.json
.venv/bin/python scripts/summarize_market_regime_observer.py /tmp/cryptobot_regime_checkpoint.json --top 10
.venv/bin/python scripts/summarize_strategy_journal.py logs/strategy_observations.jsonl --top 10
.venv/bin/python scripts/run_bybit_demo_lifecycle.py --partial-fill-probe-only --max-notional 15 --wait 8 --partial-fill-dynamic-candidates 10 --partial-fill-max-scan 100 --partial-fill-target-notional-pct 0.95
.venv/bin/python scripts/run_bybit_demo_lifecycle.py --partial-fill-probe-only --max-notional 25 --wait 8 --partial-fill-dynamic-candidates 10 --partial-fill-max-scan 250 --partial-fill-target-notional-pct 0.95 --partial-fill-price-levels 5 --partial-fill-orderbook-depth 50 --partial-fill-poll-interval 0.1
```

Guarded `main.py` demo launch:

```bash
# Full main.py observation without new order submission:
EXECUTION_ENABLED=false MULTI_STRATEGY_READ_ONLY=true BYBIT_DEMO=true BYBIT_TESTNET=false .venv/bin/python main.py

# Guarded SMC-only demo trading with runtime/order caps:
BYBIT_DEMO=true BYBIT_TESTNET=false .venv/bin/python main.py

# Guarded SMC + selected alternative-strategy demo trading:
BYBIT_DEMO=true BYBIT_TESTNET=false EXECUTION_ENABLED=true \
MULTI_STRATEGY_READ_ONLY=true MULTI_STRATEGY_EXECUTION_ENABLED=true \
MULTI_STRATEGY_ALLOWED_STRATEGIES=MEAN_REVERSION,BREAKOUT,TREND_PULLBACK,VOLATILITY_EXPANSION \
MAX_RUNTIME_MINUTES=30 MAX_ORDERS_PER_RUN=1 MAX_ORDERS_PER_CYCLE=1 MAX_ORDER_NOTIONAL_USD=25 \
.venv/bin/python main.py
```

`main.py` refuses live trading unless
`RISK_MANAGEMENT["global"]["allow_live_trading"]` is explicitly set to `True`.
The default runtime guard allows only one new entry per run, one new entry per
cycle, a 25 USDT max order notional, and a 30 minute process runtime while
active position management remains enabled during the run. The global drawdown
breaker stops the process when equity falls through
`max_drawdown_limit_pct`. Set `EXECUTION_ENABLED=false` for full `main.py`
market observation without new order submission; active position management and
read-only strategy telemetry still run. Significant alternative-strategy
decisions are written to `logs/strategy_observations.jsonl`; summarize them with
`scripts/summarize_strategy_journal.py`. `start.sh` uses the existing `.venv`,
defaults to demo mode, and does not restart the bot unless `MAX_RESTARTS` is set.

`run_bybit_demo_lifecycle.py` fails closed unless `BYBIT_DEMO=true` or
`BYBIT_TESTNET=true`. It covers safe create/amend/cancel, expected retCode
failures, partial reduce-only close, reduce-only TP, stop-loss set/clear,
restart recovery sync, and a best-effort partial-fill probe that always cleans
up its own orders/positions. `run_bybit_demo_lifecycle_soak.py` repeats that
full lifecycle and validates the required steps after each iteration. The
`run_strategy_observer.py` script is read-only: it fails closed outside
demo/testnet by default, does not import the executor, does not call
`place_order`, and emits JSON score/signal observations only. Use
`--summary-only` for longer read-only soaks; it keeps status/reason/near-setup
counts, signal route counts, failed checks, and repeated-symbol frequencies
without printing every per-symbol analysis packet. Compact setup summaries
include blocker details for POI, M5 trigger, P/D alignment, and liquidity
target checks, plus aggregate blocker-detail counts. Observer liquidity target
diagnostics mirror scoring fallback rules for EQH/EQL/QL. Signal outputs
include a read-only plan with reference entry/SL/TP/R:R and planned limit/market entry,
plus protective stop/R:R after the minimum stop-distance guard, but no quantity
and no exchange order placement. High-score setups that still lack M5
confirmation are reported as `WAIT_CONFIRMATION`, not execution-ready
`SIGNAL`. The probe-only mode
dynamically ranks low-notional
USDT instruments by visible orderbook size, attempts a capped demo/testnet
partial fill near `--max-notional * --partial-fill-target-notional-pct`, can
cross multiple ask levels with `--partial-fill-price-levels`, sweeps down to
shallower levels when deeper visible liquidity exceeds the cap, polls quickly
after placement to catch transient partial-fill states, and exits before the
broader lifecycle.

`run_market_regime_observer.py` is also read-only and exists to prevent the bot
from becoming a low-quality "trade anything" system. It classifies each symbol
as `TRENDING`, `RANGE`, `LOW_VOL_COMPRESSION`, `CHOP`, or `DATA_ERROR`. Trend
regimes stay assigned to the existing SMC engine. Range regimes only produce
`RANGE_EDGE_WATCH` near confirmed range edges; mid-range prices are explicitly
`RANGE_MID_NO_TRADE`. Low-volatility compression produces `WAIT_BREAKOUT` and
requires range break, volume expansion, and retest before any future execution
logic should be considered. The script never imports the executor and never
places orders. It now also runs a read-only `MEAN_REVERSION` candidate detector
for `RANGE_EDGE_WATCH` setups and a read-only strategy coordinator. Mean
reversion candidates require edge touch, reclaim back inside the range, rejection
from the edge, controlled trigger volume, and minimum R:R before they become
`WATCH_ONLY`. It also runs a read-only `BREAKOUT` candidate detector for
`LOW_VOL_COMPRESSION` setups. Breakout candidates require a close outside the
compression range, volume expansion, impulse body quality, controlled extension,
hold beyond the broken edge, retest, and minimum R:R before they become
`WATCH_ONLY`. It also runs read-only `TREND_PULLBACK` candidates for `TRENDING`
regimes. Trend pullback requires 1h trend alignment, 15m value-area
pullback/reclaim, controlled pullback volume, 5m continuation trigger, and
minimum R:R. It observes trend continuation without changing the existing SMC
execution path. It also runs read-only `VOLATILITY_EXPANSION` candidates across
non-error regimes. Volatility expansion requires ATR expansion, volume expansion,
an impulsive local range break, controlled extension, non-opposed 1h context,
5m continuation quality, and minimum R:R. This is for finding fresh activity
bursts without chasing already overextended candles. The coordinator can select
only one watch candidate per symbol and emits `NO_ACTION`, `WATCH_ONLY`, or
`CONFLICT_NO_ACTION`; it still cannot place or size an order. Coordinator
selection now validates each candidate plan, blocks invalid directional
entry/stop/target layouts, rejects candidates below the coordinator minimum R:R,
reports rejected candidate reasons, and prefers the strategy designed for the
current regime before using score/R:R as tie-breakers. Long unattended observer
runs can use `--progress-jsonl` to append one compact cycle summary
after every completed cycle,
`--checkpoint-output` to keep a rolling partial aggregate that survives
interruption, and `--final-output` to write the final JSON summary when the run
finishes. A full-cycle failure is recorded as `CYCLE_ERROR` and the observer
continues to the next interval instead of dropping the whole overnight run.
`summarize_market_regime_observer.py` reads either the rolling checkpoint, final
JSON, or progress JSONL and emits a compact decision report with regime counts,
Mean Reversion, Breakout, Trend Pullback, and Volatility Expansion watch totals,
coordinator selections, strategy failed-check/reason aggregates, top repeated
symbol/status blockers, cycle errors, and the next recommended review action.
`main.py` can also run the same multi-strategy analysis as read-only telemetry
through `MULTI_STRATEGY_READ_ONLY=true`. In that mode the main SMC execution
path does not change: alternative strategies can only log `ALT_WATCH`,
`ALT_CONFLICT`, or `ALT_REJECT` summary states and cannot submit orders.
Alternative strategy execution is a separate explicit mode:
`MULTI_STRATEGY_EXECUTION_ENABLED=true`. When enabled, the coordinator-selected
Mean Reversion, Breakout, Trend Pullback, or Volatility Expansion plan is
validated again for allowed strategy name, side, order type, entry/SL/target
geometry, score, and `MULTI_STRATEGY_MIN_RR` before it reaches the same
RiskManager, order caps, duplicate-position checks, notional guard, executor,
SQLite, audit, and Telegram path used by SMC. Strategy-specific targets are
passed to the executor as TP overrides instead of using the default SMC TP
ratio grid. If an alternative strategy submits an order for a symbol, the SMC
path skips that symbol for the same scan cycle to avoid duplicate entries.

CI and security checks:

```bash
.venv/bin/python scripts/secret_scan.py --history
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q core engine tests scripts main.py config.py analyze_trades.py
.venv/bin/python scripts/pre_commit_checks.py
git config core.hooksPath .githooks
```

GitHub Actions installs from `requirements-lock.txt`, runs `pip check`, scans
the full fetched history for secrets/runtime artifacts, compiles sources, and
runs the unit test suite.

`scripts/pre_commit_checks.py` is the same local guard wired by
`.githooks/pre-commit`: it scans staged content for secrets/runtime paths, runs
the full history scanner, compiles Python sources, and runs unit tests before a
commit is accepted.

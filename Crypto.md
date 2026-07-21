
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
│   └── tp_manager.py
├── data
│   └── runtime SQLite/JSONL files (ignored by git)
├── engine
│   ├── __init__.py
│   ├── filters.py
│   ├── indicators.py
│   ├── liquidity.py
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
│   ├── run_paper_lifecycle.py
│   ├── run_strategy_observer.py
│   └── secret_scan.py
├── tests
│   ├── test_bybit_demo_lifecycle.py
│   ├── test_bybit_demo_soak.py
│   ├── test_execution_safety.py
│   ├── test_paper_lifecycle.py
│   ├── test_pre_commit_checks.py
│   ├── test_secret_scan.py
│   └── test_strategy_observer.py

Safe lifecycle checks:

```bash
python3 scripts/run_paper_lifecycle.py --db /tmp/cryptobot_paper_lifecycle.db --reset-db
python3 scripts/run_paper_lifecycle.py --db /tmp/cryptobot_paper_partial_lifecycle.db --reset-db --partial-fill-recovery
.venv/bin/python scripts/run_bybit_demo_lifecycle.py --symbol XRPUSDT --max-notional 15 --wait 20
.venv/bin/python scripts/run_bybit_demo_lifecycle_soak.py --iterations 3 --symbol XRPUSDT --max-notional 25 --wait 20 --sleep 3
.venv/bin/python scripts/run_strategy_observer.py --cycles 3 --sleep 60 --max-symbols 5
.venv/bin/python scripts/run_strategy_observer.py --cycles 10 --sleep 60 --max-symbols 0 --summary-only
.venv/bin/python scripts/run_bybit_demo_lifecycle.py --partial-fill-probe-only --max-notional 15 --wait 8 --partial-fill-dynamic-candidates 10 --partial-fill-max-scan 100 --partial-fill-target-notional-pct 0.95
.venv/bin/python scripts/run_bybit_demo_lifecycle.py --partial-fill-probe-only --max-notional 25 --wait 8 --partial-fill-dynamic-candidates 10 --partial-fill-max-scan 250 --partial-fill-target-notional-pct 0.95 --partial-fill-price-levels 5 --partial-fill-orderbook-depth 50 --partial-fill-poll-interval 0.1
```

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

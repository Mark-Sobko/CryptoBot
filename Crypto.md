
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
├── requirements.txt
├── scripts
│   ├── run_bybit_demo_lifecycle.py
│   └── run_paper_lifecycle.py
├── tests
│   ├── test_execution_safety.py
│   └── test_paper_lifecycle.py

Safe lifecycle checks:

```bash
python3 scripts/run_paper_lifecycle.py --db /tmp/cryptobot_paper_lifecycle.db --reset-db
.venv/bin/python scripts/run_bybit_demo_lifecycle.py --symbol XRPUSDT --max-notional 15
```

`run_bybit_demo_lifecycle.py` fails closed unless `BYBIT_DEMO=true` or
`BYBIT_TESTNET=true`.


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

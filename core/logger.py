import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Dict, Any, List

import config


class TradeLogger:
    """
    [INSTITUTIONAL AUDIT SYSTEM v4.7]
    JSONL-аудит сделок + системные логи с ротацией.
    Расширен: добавлено безопасное чтение хвоста лога для удаленного мониторинга.
    """

    def __init__(self):
        self.path = str(config.HISTORY_PATH)
        self.system_log = str(config.LOG_PATH)
        self._lock = threading.Lock()

        self._ensure_dirs()

        self.logger = logging.getLogger("SMC_BOT")
        self._setup_system_logging()

    def _ensure_dirs(self) -> None:
        os.makedirs(str(config.DATA_DIR), exist_ok=True)

        if self.system_log:
            os.makedirs(os.path.dirname(self.system_log), exist_ok=True)

    def _setup_system_logging(self) -> None:
        self.logger.setLevel(config.LOG_LEVEL)
        self.logger.propagate = False

        if not self.logger.handlers:
            formatter = logging.Formatter(config.LOG_FORMAT)

            file_handler = RotatingFileHandler(
                self.system_log,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(config.LOG_LEVEL)

            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            console_handler.setLevel(config.LOG_LEVEL)

            self.logger.addHandler(file_handler)
            self.logger.addHandler(console_handler)

        logging.getLogger("pybit").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)

    def log_trade_event(
        self,
        event_type: str,
        symbol: str,
        data: Dict[str, Any],
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": str(event_type),
            "symbol": str(symbol),
            "data": data if isinstance(data, dict) else {"value": str(data)},
        }

        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))

        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
        except Exception as e:
            print(
                f"🚨 [CRITICAL AUDIT WRITE ERROR] JSONL write failed: {e}",
                file=sys.stderr,
            )

    def log_attempt(
        self,
        symbol: str,
        score: int,
        result: str,
        details: str,
    ) -> None:
        data = {
            "score": int(score),
            "result": str(result),
            "details": str(details),
        }

        self.log_trade_event("TRADE_ATTEMPT", symbol, data)

        icon = "🚀" if result == "ENTERED" else "🔘"
        self.logger.info(
            f"{icon} [{result}] {symbol} | Score: {score} | Reason: {details}"
        )

    def info(self, message: str) -> None:
        self.logger.info(str(message))

    def warning(self, message: str) -> None:
        self.logger.warning(str(message))

    def debug(self, message: str) -> None:
        self.logger.debug(str(message))

    def error(self, message: str, exc_info: bool = False) -> None:
        self.logger.error(str(message), exc_info=exc_info)

    def get_daily_stats(self) -> Dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()

        stats: Dict[str, Any] = {
            "entries": 0,
            "skips": 0,
            "signals": 0,
            "errors": 0,
            "pnl_usd": 0.0,
            "wins": 0,
            "losses": 0,
        }

        if not os.path.exists(self.path):
            return stats

        try:
            with self._lock:
                with open(self.path, "r", encoding="utf-8") as f:
                    for raw_line in f:
                        line = raw_line.strip()

                        if not line:
                            continue

                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # Безопасная проверка метки времени
                        ts_str = str(event.get("ts", ""))
                        if not ts_str.startswith(today):
                            continue

                        event_type = event.get("type")
                        data = event.get("data", {}) or {}

                        if event_type == "TRADE_ATTEMPT":
                            result = data.get("result")

                            if result == "ENTERED":
                                stats["entries"] += 1
                            else:
                                stats["skips"] += 1

                        elif event_type == "SIGNAL":
                            stats["signals"] += 1

                        elif event_type == "ERROR":
                            stats["errors"] += 1

                        elif event_type in ("TRADE_CLOSED", "PNL"):
                            try:
                                pnl = float(data.get("pnl_usd", 0.0) or 0.0)
                                stats["pnl_usd"] += pnl

                                if pnl > 0:
                                    stats["wins"] += 1
                                elif pnl < 0:
                                    stats["losses"] += 1
                            except (ValueError, TypeError):
                                continue

            return stats

        except Exception as e:
            print(f"🚨 [KPI ERROR] Daily stats parse failed: {e}", file=sys.stderr)
            return stats

    # =========================================================================
    # [INSTITUTIONAL SCALING] Чтение хвоста логов (Tail Log) для мониторинга
    # =========================================================================
    def get_last_logs(self, lines: int = 20) -> List[str]:
        """
        Возвращает последние N строк системного лог-файла.
        Полезно для вызова через Telegram-команду /logs, чтобы узнать статус системы.
        """
        if not os.path.exists(self.system_log):
            return ["Лог файл пуст или не создан."]
            
        try:
            with self._lock:
                with open(self.system_log, "r", encoding="utf-8") as f:
                    # Читаем все строки и берем с конца
                    content = f.readlines()
                    tail = content[-lines:] if len(content) > lines else content
                    return [line.strip() for line in tail]
        except Exception as e:
            self.logger.error(f"Failed to read tail logs: {e}")
            return [f"Ошибка чтения логов: {e}"]
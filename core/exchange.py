import logging
import time
from typing import Dict, List, Optional, Any

import pandas as pd
from pybit.unified_trading import HTTP

import config
from core.notifier import TelegramNotifier


class ExchangeManager:
    """
    [INSTITUTIONAL EXCHANGE BRIDGE v4.7]
    Надежный мост к Bybit V5 UTA:
    - строгая проверка API-ответов
    - retry/backoff
    - корректный UTA available balance
    - очистка свечей от NaN
    - синхронизация lookback с config.SMC_SETTINGS
    Расширен: добавлено получение спецификаций тикера (tick_size/qty_step)
    и методы исполнения (set_leverage, place_order).
    """

    CATEGORY = "linear"
    ACCOUNT_TYPE = "UNIFIED"
    SETTLE_COIN = "USDT"

    def __init__(self):
        self.logger = logging.getLogger("ExchangeManager")
        self.notifier = TelegramNotifier()

        global_cfg = config.RISK_MANAGEMENT.get("global", {})
        self.retry_attempts = int(global_cfg.get("retry_attempts", 5))
        self.request_timeout = int(global_cfg.get("request_timeout", 60))

        self.session = HTTP(
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
            demo=getattr(config, "BYBIT_DEMO", True),
            testnet=getattr(config, "BYBIT_TESTNET", False),
            recv_window=20000,
            timeout=self.request_timeout
        )

        try:
            balance = self.get_total_balance()
            self.logger.info(
                f"💰 [EXCHANGE INIT] Bybit UTA equity: {balance:.2f} USDT"
            )
        except Exception as e:
            self.logger.error(f"⚠️ [EXCHANGE INIT WARNING] Bybit init check failed: {e}")

    @staticmethod
    def _is_success(res: Dict[str, Any]) -> bool:
        return isinstance(res, dict) and res.get("retCode") == 0

    @staticmethod
    def _normalize_position_side(side: str) -> str:
        side_upper = str(side).upper().strip()
        if side_upper in ("BUY", "LONG"):
            return "LONG"
        if side_upper in ("SELL", "SHORT"):
            return "SHORT"
        return side_upper

    def _request_with_retry(self, func, *args, **kwargs) -> Optional[Dict[str, Any]]:
        last_error = None

        for attempt in range(1, self.retry_attempts + 1):
            try:
                res = func(*args, **kwargs)

                if self._is_success(res):
                    return res

                ret_msg = res.get("retMsg", "Unknown error") if isinstance(res, dict) else str(res)
                self.logger.warning(f"⚠️ [BYBIT API] Attempt {attempt}/{self.retry_attempts} failed: {ret_msg}")

            except Exception as e:
                last_error = e
                # Улучшенная диагностика сетевых проблем
                error_str = str(e).lower()
                is_network_error = "nodename" in error_str or "connection" in error_str or "timeout" in error_str
                
                self.logger.warning(
                    f"⚠️ [BYBIT API EXCEPTION] Attempt {attempt}/{self.retry_attempts}: {e}"
                )
                
                # Если это сеть, делаем паузу длиннее, чем для API-ошибок
                if is_network_error:
                    time.sleep(10) 
                    continue

            # Стандартная задержка для остальных случаев
            if attempt < self.retry_attempts:
                time.sleep(min(2 ** attempt, 10))

        if last_error:
            self.logger.error(f"🛑 [BYBIT API FAILED] Final exception: {last_error}")

        return None

    def get_total_balance(self) -> float:
        """Общий equity UTA-аккаунта."""
        res = self._request_with_retry(
            self.session.get_wallet_balance,
            accountType=self.ACCOUNT_TYPE,
            coin=self.SETTLE_COIN
        )

        if not res:
            return 0.0

        try:
            account = res["result"]["list"][0]
            return float(account.get("totalEquity", 0.0))
        except Exception as e:
            self.logger.error(f"🛑 [BALANCE PARSE ERROR] {e}")
            return 0.0

    def get_available_balance(self) -> float:
        """Свободная маржа UTA для открытия новых позиций."""
        res = self._request_with_retry(
            self.session.get_wallet_balance,
            accountType=self.ACCOUNT_TYPE,
            coin=self.SETTLE_COIN
        )

        if not res:
            return 0.0

        try:
            account = res["result"]["list"][0]
            return float(
                account.get(
                    "totalAvailableBalance",
                    account.get("availableToWithdraw", account.get("walletBalance", 0.0))
                )
            )
        except Exception as e:
            self.logger.error(f"🛑 [AVAILABLE BALANCE PARSE ERROR] {e}")
            return 0.0

    def get_active_positions(self) -> List[Dict[str, Any]]:
        """Список открытых linear USDT-позиций."""
        res = self._request_with_retry(
            self.session.get_positions,
            category=self.CATEGORY,
            settleCoin=self.SETTLE_COIN
        )

        if not res:
            return []

        active_positions: List[Dict[str, Any]] = []

        try:
            for p in res["result"]["list"]:
                size = float(p.get("size", 0) or 0)

                if size <= 0:
                    continue

                entry_price = float(p.get("entryPrice", 0) or 0)
                mark_price = float(p.get("markPrice", 0) or 0)
                stop_loss = float(p.get("stopLoss", 0) or 0)

                active_positions.append({
                    "symbol": p.get("symbol"),
                    "side": p.get("side"),
                    "size": size,
                    "entry_price": entry_price,
                    "entryPrice": entry_price,
                    "mark_price": mark_price,
                    "markPrice": mark_price,
                    "stop_loss": stop_loss,
                    "stopLoss": stop_loss,
                    "positionIdx": int(p.get("positionIdx", 0) or 0),
                    "unrealisedPnl": float(p.get("unrealisedPnl", 0) or 0),
                    "leverage": float(p.get("leverage", 0) or 0),
                })

        except Exception as e:
            self.logger.error(f"🛑 [POSITIONS PARSE ERROR] {e}")
            return []

        return active_positions

    def can_open_new_trade(self, max_trades: int) -> bool:
        active_count = len(self.get_active_positions())

        if active_count >= max_trades:
            self.logger.warning(
                f"⚠️ [EXCHANGE LIMIT] Open positions limit reached: "
                f"{active_count}/{max_trades}"
            )
            return False

        return True

    def sync_db_with_exchange(self, db_instance) -> None:
        """
        [INSTITUTIONAL SYNC]
        Синхронизирует активные позиции биржи с базой данных.
        Pending limit orders stay pending until a real exchange position appears.
        """
        try:
            active_on_exchange = self.get_active_positions()
            active_symbols_on_exchange = {p["symbol"] for p in active_on_exchange}

            for pos in active_on_exchange:
                symbol = str(pos.get("symbol", ""))
                side = self._normalize_position_side(str(pos.get("side", "")))
                db_instance.mark_trade_open(
                    symbol=symbol,
                    side=side,
                    entry_price=float(pos.get("entry_price", pos.get("entryPrice", 0.0)) or 0.0),
                    qty=float(pos.get("size", 0.0) or 0.0),
                    stop_loss=float(pos.get("stop_loss", pos.get("stopLoss", 0.0)) or 0.0),
                )
            
            open_in_db = db_instance.get_open_positions()
            
            for trade in open_in_db:
                if trade['symbol'] not in active_symbols_on_exchange:
                    self.logger.warning(f"🔄 [SYNC] Сделка {trade['symbol']} (ID: {trade['id']}) не найдена на бирже. Закрываем в БД.")
                    
                    # Закрываем сделку в базе данных
                    db_instance.close_trade(
                        symbol=trade['symbol'],
                        exit_price=0.0, 
                        pnl_usd=0.0,    
                        pnl_pct=0.0,
                        trade_id=trade['id']
                    )

            for trade in getattr(db_instance, "get_pending_orders", lambda: [])():
                symbol = str(trade.get("symbol", ""))
                if symbol in active_symbols_on_exchange:
                    continue

                order_id = str(trade.get("order_id", "") or "")
                if not order_id:
                    continue

                res = self._request_with_retry(
                    self.session.get_open_orders,
                    category=self.CATEGORY,
                    symbol=symbol,
                )

                if not res:
                    continue

                open_orders = res.get("result", {}).get("list", [])
                still_pending = any(str(order.get("orderId", "")) == order_id for order in open_orders)

                if not still_pending:
                    self.logger.warning(
                        f"🔄 [SYNC] Pending order {symbol} (ID: {trade['id']}) is no longer open. Marking CANCELLED."
                    )
                    db_instance.mark_trade_cancelled(
                        symbol=symbol,
                        order_id=order_id,
                        trade_id=trade.get("id"),
                    )
        except Exception as e:
            self.logger.error(f"❌ [SYNC ERROR] Ошибка синхронизации БД: {e}")

    def fetch_all_timeframes(self, symbol: str) -> Optional[Dict[str, pd.DataFrame]]:
        """Пакет свечей 5m / 15m / 1h / 4h."""
        timeframes = {
            "5m": "5",
            "15m": "15",
            "1h": "60",
            "4h": "240"
        }

        packet: Dict[str, pd.DataFrame] = {}

        for name, interval in timeframes.items():
            df = self._fetch_candles(symbol, interval)

            if df is None or df.empty:
                self.logger.warning(
                    f"❌ [MTF PACKET] Failed timeframe {name} for {symbol}"
                )
                return None

            packet[name] = df

        return packet

    def _fetch_candles(
        self,
        symbol: str,
        interval: str,
        limit: Optional[int] = None
    ) -> Optional[pd.DataFrame]:
        """Загрузка и очистка свечей Bybit."""
        if limit is None:
            limit = int(config.SMC_SETTINGS.get("lookback_bars", 500)) + 200

        res = self._request_with_retry(
            self.session.get_kline,
            category=self.CATEGORY,
            symbol=symbol,
            interval=interval,
            limit=limit
        )

        if not res:
            return None

        try:
            raw = res["result"]["list"]

            if not raw:
                self.logger.warning(f"⚠️ [KLINE EMPTY] {symbol} {interval}")
                return None

            df = pd.DataFrame(
                raw,
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "turnover"
                ]
            )

            df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")

            numeric_cols = ["open", "high", "low", "close", "volume", "turnover"]
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.dropna(
                subset=["timestamp", "open", "high", "low", "close", "volume"]
            )

            if df.empty:
                self.logger.warning(f"⚠️ [KLINE INVALID] Empty after cleanup: {symbol} {interval}")
                return None

            df = df.sort_values("timestamp", ascending=True).reset_index(drop=True)
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

            return df

        except Exception as e:
            self.logger.error(f"🛑 [KLINE PARSE ERROR] {symbol} {interval}: {e}")
            return None

    def fetch_macro_indices(self) -> Dict[str, float]:
        """BTC контекст как базовый рыночный фильтр."""
        context = {
            "BTC_trend": 0.0,
            "DXY_trend": 0.0
        }

        df = self._fetch_candles("BTCUSDT", "60", limit=24)

        if df is None or df.empty or len(df) < 2:
            return context

        try:
            close_now = float(df["close"].iloc[-1])
            close_past = float(df["close"].iloc[0])

            if close_past > 0:
                context["BTC_trend"] = ((close_now - close_past) / close_past) * 100.0

        except Exception as e:
            self.logger.warning(f"⚠️ [MACRO CONTEXT ERROR] {e}")

        return context

    # =========================================================================
    # [INSTITUTIONAL SCALING] Методы исполнения сделок (Execution Module)
    # =========================================================================

    def get_ticker_info(self, symbol: str) -> Optional[Dict[str, float]]:
        """
        Запрашивает tickSize (шаг цены) и qtyStep (шаг объема) для корректного округления.
        Предотвращает ошибку биржи "invalid qty" или "invalid price".
        """
        res = self._request_with_retry(
            self.session.get_instruments_info,
            category=self.CATEGORY,
            symbol=symbol
        )

        if not res:
            return None
            
        try:
            info = res["result"]["list"][0]
            return {
                "tickSize": float(info["priceFilter"]["tickSize"]),
                "qtyStep": float(info["lotSizeFilter"]["qtyStep"]),
                "minQty": float(info["lotSizeFilter"]["minOrderQty"])
            }
        except Exception as e:
            self.logger.error(f"🛑 [TICKER INFO ERROR] {symbol}: {e}")
            return None

    def set_leverage(self, symbol: str, leverage: float) -> bool:
        """
        Устанавливает кредитное плечо для пары (Cross Margin).
        Биржа вернет ошибку, если плечо уже равно запрашиваемому - это нормально.
        """
        str_leverage = str(int(leverage))
        try:
            res = self.session.set_leverage(
                category=self.CATEGORY,
                symbol=symbol,
                buyLeverage=str_leverage,
                sellLeverage=str_leverage
            )
            
            if self._is_success(res):
                self.logger.info(f"⚙️ [LEVERAGE] Set to {str_leverage}x for {symbol}")
                return True
                
            # Если плечо не изменено, retCode 110043 значит "плечо не изменилось" (уже установлено)
            if res.get("retCode") == 110043:
                return True
                
            self.logger.warning(f"⚠️ [LEVERAGE FAILED] {symbol}: {res.get('retMsg')}")
            return False
            
        except Exception as e:
            # Игнорируем ошибку "плечо не изменилось", перехватываемую как исключение в некоторых версиях pybit
            if "leverage not modified" in str(e).lower() or "110043" in str(e):
                return True
            self.logger.error(f"🛑 [LEVERAGE EXCEPTION] {symbol}: {e}")
            return False

    def place_order(
        self, 
        symbol: str, 
        side: str, 
        qty: float, 
        order_type: str = "Market", 
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Отправляет ордер на биржу.
        Поддерживает прикрепление SL/TP.
        """
        order_params = {
            "category": self.CATEGORY,
            "symbol": symbol,
            "side": side.capitalize(), # Buy или Sell
            "orderType": order_type.capitalize(), # Market или Limit
            "qty": str(qty),
            "timeInForce": "GTC" if order_type.upper() == "LIMIT" else "IOC"
        }
        
        if price and order_type.upper() == "LIMIT":
            order_params["price"] = str(price)
            
        if stop_loss:
            order_params["stopLoss"] = str(stop_loss)
            
        if take_profit:
            order_params["takeProfit"] = str(take_profit)

        res = self._request_with_retry(
            self.session.place_order,
            **order_params
        )

        if not res:
            self.logger.error(f"🛑 [ORDER FAILED] {symbol} {side} {qty}")
            return None

        self.logger.info(f"✅ [ORDER PLACED] {symbol} {side} Qty: {qty} | ID: {res['result']['orderId']}")
        return res["result"]

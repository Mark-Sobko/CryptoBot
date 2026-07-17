import logging
import time
import datetime
import signal
from typing import Dict, Any, Optional

import pandas as pd

import config
from core.database import TradeDatabase
from core.exchange import ExchangeManager
from core.executor import TradeExecutor
from core.logger import TradeLogger
from core.notifier import TelegramNotifier
from core.risk_manager import RiskManager

from engine.filters import MarketFilters
from engine.trend_engine import TrendEngine
from engine.smc_analyzer import SMCAnalyzer
from engine.liquidity import LiquidityEngine
from engine.scoring import ScoringSystem
from engine.indicators import ConfirmationModule
from engine.stats_analyzer import StatsAnalyzer
from engine.news_filter import NewsFilter


class InstitutionalBot:
    def __init__(self):
        self.audit = TradeLogger()
        self.logger = logging.getLogger("SMC_BOT.MainEngine")

        self.db = TradeDatabase()
        self.ex = ExchangeManager()
        self.notifier = TelegramNotifier()
        self.executor = TradeExecutor(self.ex)

        initial_balance = self.ex.get_total_balance()
        if initial_balance <= 0:
            raise ValueError("Exchange balance is zero or unavailable")

        self.risk_manager = RiskManager(balance=initial_balance)

        self.filters = MarketFilters()
        self.smc = SMCAnalyzer()
        self.liquidity = LiquidityEngine()
        self.scoring = ScoringSystem()
        self.confirmation = ConfirmationModule()
        self.stats_analyzer = StatsAnalyzer(config.DB_PATH, initial_balance=initial_balance)

        self.is_running = True
        self.last_breaker_time: Optional[datetime.datetime] = None

        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

        self.news_filter = NewsFilter()
        self.logger.info("🤖 [INIT] InstitutionalBot initialized successfully")

    def _handle_exit(self, signum, frame) -> None:
        self.logger.info("--- [SYSTEM SHUTDOWN] Stop signal received ---")
        self.is_running = False

    def run(self) -> None:
        self.logger.info("--- [SYSTEM START] SMC Institutional Alpha v5.0 ---")
        self.notifier.send_message(
            "🚀 <b>Бот запущен.</b> Режим анализа: Multi-Timeframe SMC v5.0."
        )

        while self.is_running:
            try:
                current_balance = self.ex.get_total_balance()
                if current_balance > 0:
                    self.risk_manager.balance = current_balance
                else:
                    current_balance = self.risk_manager.balance

                risk_cfg = config.get_current_risk()
                now = datetime.datetime.now(datetime.timezone.utc)

                self._manage_active_trades()

                current_pnl_usd = self.db.get_today_pnl_usd()
                max_daily_loss_usd = current_balance * (
                    float(risk_cfg["max_daily_loss_pct"]) / 100.0
                )

                if current_pnl_usd <= -max_daily_loss_usd:
                    if (
                        self.last_breaker_time is None
                        or (now - self.last_breaker_time).total_seconds() > 3600
                    ):
                        msg = (
                            f"🚨 <b>Daily Loss Limit Reached</b>\n"
                            f"PnL: <code>{current_pnl_usd:.2f} USDT</code>\n"
                            f"Limit: <code>-{max_daily_loss_usd:.2f} USDT</code>\n"
                            f"Сканирование заблокировано. Сопровождение активно."
                        )
                        self.logger.critical(msg)
                        self.notifier.send_message(msg)
                        self.last_breaker_time = now

                    self._sleep_interruptible(60)
                    continue

                self.last_breaker_time = None

                self._scan_market(risk_cfg, current_balance)

                wait_time = self._calculate_cooldown()
                self.logger.info(f"Цикл завершен. Пауза {wait_time // 60} мин.")
                self._sleep_interruptible(wait_time)

            except Exception as e:
                self.logger.error(f"Main loop critical error: {e}", exc_info=True)
                self.audit.error(f"Critical Exception: {str(e)}")
                self._sleep_interruptible(60)

        self._shutdown()

    def _sleep_interruptible(self, seconds: int) -> None:
        for _ in range(int(seconds)):
            if not self.is_running:
                break
            time.sleep(1)

    def _shutdown(self) -> None:
        try:
            if hasattr(self.db, "close"):
                self.db.close()
        except Exception as e:
            self.logger.error(f"DB shutdown error: {e}")

        self.logger.info("✅ Bot stopped gracefully")

    def _manage_active_trades(self) -> None:
        try:
            positions = self.ex.get_active_positions()
            for pos in positions:
                self.executor.manage_position_pro(pos)
        except Exception as e:
            self.logger.error(f"Position management error: {e}", exc_info=True)

    def _scan_market(self, risk_cfg: Dict[str, Any], current_balance: float) -> None:
        self.ex.sync_db_with_exchange(self.db) 
        summary_list = []

        news = self.news_filter.analyze_news()

        if news["action"] != "NONE":
            msg = f"📰 <b>News Analysis:</b> {news['action']}\nTitle: {news['title']}"
            self.notifier.send_message(msg)
        
        if news["action"] == "BLOCK":
            self.logger.warning("🛑 [NEWS BLOCK] Торговля запрещена новостью.")
            return

        for symbol in config.SYMBOLS:
            if not self.is_running:
                break

            if symbol in config.BLACKLIST:
                continue
        
            try:
                data = self.ex.fetch_all_timeframes(symbol)
                if not self._validate_market_data(symbol, data):
                    continue

                trend = TrendEngine.get_direction(data["1h"], data["15m"])

                if not self.filters.is_market_suitable(data["1h"]):
                    rel_vol = getattr(self.filters, "last_rel_vol", 0.0)
                    summary_list.append({
                        "symbol": symbol,
                        "status": "REJECT",
                        "reason": "Низкий объем/Шум",
                        "rel_vol": rel_vol,
                    })
                    continue
                
                rel_vol = getattr(self.filters, "last_rel_vol", 0.0)

                if trend == "FLAT":
                    summary_list.append({
                        "symbol": symbol,
                        "status": "FLAT",
                        "rel_vol": rel_vol,
                    })
                    continue
                # --- 4. Теперь применяем влияние новостей на Score ---
                score_bonus = 0
                if news["action"] == "LONG" and trend == "LONG":
                    score_bonus = 15
                elif news["action"] == "SHORT" and trend == "SHORT":
                    score_bonus = 15
                elif news["action"] != "NONE" and news["action"] != trend:
                    score_bonus = -20

                self.logger.info(f"🔍 {symbol} | Trend={trend} | SMC scan started")

                # =====================================================================
                # [INSTITUTIONAL SCALING] Интеграция расширенных модулей анализа
                # =====================================================================
                
                # 1. Используем фрактальный MTF анализ вместо ручной сборки
                mtf_context = self.smc.analyze_mtf(df_htf=data["1h"], df_ltf=data["15m"])
                
                final_structure = mtf_context.get("ltf_structure") if mtf_context.get("ltf_structure", {}).get("is_confirmed") else mtf_context.get("htf_structure", {})
                final_poi = mtf_context.get("poi")

                # 2. Получаем глубокую оценку ликвидности
                liquidity_15m = self.liquidity.analyze(data["15m"])
                sweep_5m = self.liquidity.check_sweep_pro(data["5m"])
                liq_quality = self.liquidity.evaluate_liquidity_context(liquidity_15m)

                macro = self.ex.fetch_macro_indices()
                macro_ok = MarketFilters.check_macro(macro, trend)

                # Используем вычисленный в фасаде параметр выравнивания
                pd_aligned = mtf_context.get("is_pd_aligned", False)

                has_liquidity_target = mtf_context.get("has_liquidity_target", False)

                # 3. Собираем обогащенный словарь для ScoringSystem
                analysis = {
                    "trend": trend,
                    "direction": trend,
                    "trend_ok": True,
                    "structure_ok": bool(final_structure.get("is_confirmed", False)),
                    "poi_ok": final_poi is not None,
                    "m5_ok": self.confirmation.check_m5_entry(data["5m"], trend),
                    "macro_ok": macro_ok,
                    "liquidity_sweep": bool(sweep_5m.get("is_confirmed", False)),
                    "sweep_active": bool(sweep_5m.get("is_confirmed", False)),
                    "is_pd_aligned": pd_aligned,
                    "has_liquidity_target": has_liquidity_target,
                    "has_eqh": liquidity_15m.get("has_eqh", False),
                    "has_eql": liquidity_15m.get("has_eql", False),
                    "has_ql": liquidity_15m.get("has_ql", False),
                    "high_volatility": False,
                    # Расширенные контексты
                    "news_action": news["action"],
                    "liquidity_context": liq_quality
                }

                poi_side_aligned = bool(final_poi and final_poi.get("side") == trend)
                analysis["poi_ok"] = poi_side_aligned and bool(mtf_context.get("smc_ok", False))

                score = max(0, min(100, self.scoring.calculate(analysis) + score_bonus))

                poi_status = "✅" if analysis["poi_ok"] else "❌"
                struct_status = (
                    "M15"
                    if mtf_context.get("ltf_structure", {}).get("is_confirmed")
                    else ("H1" if mtf_context.get("htf_structure", {}).get("is_confirmed") else "❌")
                )
                sweep_status = "✅" if analysis["liquidity_sweep"] else "❌"

                self.logger.info(
                    f"{'🟢' if score >= risk_cfg['min_score_to_enter'] else '🟡'} "
                    f"{symbol:10} | Score: {score:3}/{risk_cfg['min_score_to_enter']} | "
                    f"Trend: {trend:5} | POI: {poi_status} | "
                    f"Struct: {struct_status} | Sweep: {sweep_status}"
                )

                if score < risk_cfg["min_score_to_enter"]:
                    summary_list.append({
                        "symbol": symbol,
                        "status": "REJECT",
                        "reason": f"Недобор баллов ({score}/{risk_cfg['min_score_to_enter']})",
                        "rel_vol": rel_vol,
                    })
                    time.sleep(2)
                    continue

                summary_list.append({
                    "symbol": symbol,
                    "status": "SIGNAL",
                    "side": trend,
                    "score": score,
                    "rel_vol": rel_vol,
                })

                if final_poi is None:
                    self.logger.warning(f"⚠️ {symbol} | Score passed but POI is missing")
                    continue

                if final_poi.get("side") != trend:
                    self.logger.warning(
                        f"⚠️ {symbol} | POI side mismatch: poi={final_poi.get('side')} trend={trend}"
                    )
                    continue

                daily_pnl_usd = self.db.get_today_pnl_usd()
                active_positions = self.ex.get_active_positions()

                is_safe, reason = self.risk_manager.check_safety_filters(
                    daily_pnl_usd=daily_pnl_usd,
                    active_positions=active_positions,
                    symbol=symbol,
                    exchange_manager=self.ex
                )

                if not is_safe:
                    self.logger.warning(
                        f"🛑 [RISK REJECT] {symbol} rejected: {reason}"
                    )
                    continue

                # --- [ЗАЩИТА СТОП-ЛОССА] ---
                current_price = float(data["1h"]["close"].iloc[-2])
                sl_raw = (
                    final_poi.get("bottom")
                    if trend == "LONG"
                    else final_poi.get("top")
                )

                # Безопасный расчет: если POI пустой, берем отступ 0.5%
                if sl_raw is None:
                    sl_price = current_price * 1.005 if trend == "SHORT" else current_price * 0.995
                else:
                    sl_price = float(sl_raw)

                # Принудительная корректировка "кривых" стопов
                if trend == "SHORT" and sl_price <= current_price:
                    sl_price = current_price * 1.005 
                elif trend == "LONG" and sl_price >= current_price:
                    sl_price = current_price * 0.995

                zone_top = float(final_poi.get("top", current_price))
                zone_bottom = float(final_poi.get("bottom", current_price))
                
                # Защита от нулевого размера зоны
                zone_size = abs(zone_top - zone_bottom)
                if zone_size <= 0:
                    zone_size = current_price * 0.01 
                # -----------------------------

                tp_price = (
                    current_price + zone_size * 3
                    if trend == "LONG"
                    else current_price - zone_size * 3
                )

                # --- ИСПРАВЛЕНИЕ: Мягкий режим для R:R ---
                rr_status = self.risk_manager.validate_risk_reward(
                    entry=current_price,
                    stop=sl_price,
                    tp=tp_price,
                    score=score
                )

                if rr_status == "REJECT":
                    self.logger.warning(f"⚠️ {symbol} | R:R rejected (Low score fallback)")
                    continue
                

                is_limit_order = (rr_status == "LIMIT")

                if is_limit_order:
                    # ХИРУРГИЧЕСКИЙ ФИКС: Берем строго середину (Equilibrium) зоны институционального блока POI
                    execution_entry = (zone_top + zone_bottom) / 2.0
                    
                    # Пересчитываем Тейк-Профит от новой, выгодной лимитной точки входа, сохраняя R:R 1:3
                    tp_price = (
                        execution_entry + zone_size * 3
                        if trend == "LONG"
                        else execution_entry - zone_size * 3
                    )
                    self.logger.info(
                        f"🎯 [LIMIT ROUTE] {symbol} | Placing Pending LIMIT Order at POI equilibrium: {execution_entry:.5f}"
                    )
                else:
                    execution_entry = current_price

                available_balance = self.ex.get_available_balance()

                qty, corrected_sl = self.risk_manager.calculate_lot_size(
                    side=trend,
                    entry_price=execution_entry,
                    stop_loss=sl_price,
                    available_balance=available_balance,
                )

                if qty <= 0:
                    self.logger.warning(f"⚠️ {symbol} | Qty is zero after risk sizing")
                    continue

                if not self.ex.can_open_new_trade(risk_cfg["max_open_trades"]):
                    self.logger.warning(
                        f"⚠️ {symbol} | Max positions reached: {risk_cfg['max_open_trades']}"
                    )
                    continue

                entry_result = self.executor.execute_institutional_entry(
                    symbol=symbol,
                    side=trend,
                    poi=final_poi,
                    score=score,
                    qty=qty,
                    sl=corrected_sl,
                    risk_pct=risk_cfg["risk_per_trade_pct"],
                    order_type="Limit" if is_limit_order else "Market",
                    limit_price=execution_entry if is_limit_order else None
                )
                # --- КОНЕЦ ГИБРИДНОГО БЛОКА ---

                if entry_result:
                    status_text = "PENDING LIMIT" if is_limit_order else "ENTERED"
                    self.audit.log_attempt(
                        symbol,
                        score,
                        status_text,
                        f"SMC Confirmed: {final_structure.get('type', 'N/A')}",
                    )

                    metrics_data = {
                        "adx": getattr(self.filters, "last_adx", 0.0),
                        "er": getattr(self.filters, "last_er", 0.0),
                        "atr_pct": getattr(self.filters, "last_atr_pct", 0.0),
                        "rel_vol": rel_vol,
                    }

                    self.notifier.notify_signal(
                        symbol=symbol,
                        score=score,
                        side=trend,
                        price=execution_entry,
                        sl=corrected_sl,
                        tp=tp_price,
                        metrics=metrics_data,
                    )

                time.sleep(2)

            except Exception as e:
                self.logger.error(f"Symbol scan error {symbol}: {e}", exc_info=True)
                continue

        if summary_list and self.is_running:
            self._send_cycle_reports(summary_list)

    def _send_cycle_reports(self, summary_list) -> None:
        try:
            equity = self.ex.get_total_balance()
            if equity <= 0:
                equity = self.risk_manager.balance

            self.notifier.notify_market_summary(summary_list, equity)

            reports = self.stats_analyzer.generate_report_chunks()
            # [INSTITUTIONAL SCALING] Используем новый метод отправки отформатированной статистики
            self.notifier.notify_stats(reports)

        except Exception as e:
            self.logger.error(f"Telegram summary/report failed: {e}", exc_info=True)

    def _validate_market_data(self, symbol: str, data: Optional[Dict[str, pd.DataFrame]]) -> bool:
        required_tfs = ["5m", "15m", "1h", "4h"]

        if data is None or not isinstance(data, dict):
            self.logger.warning(f"⚪️ {symbol:10} | Data packet is empty")
            return False

        min_bars = {
            "5m": 80,
            "15m": int(config.SMC_SETTINGS.get("structure_lookback", 120)),
            "1h": int(config.SMC_SETTINGS.get("pd_lookback", 250)),
            "4h": 80,
        }

        required_cols = {"open", "high", "low", "close", "volume"}

        for tf in required_tfs:
            df = data.get(tf)

            if df is None or df.empty:
                self.logger.warning(f"⚪️ {symbol:10} | Missing timeframe {tf}")
                return False

            if len(df) < min_bars[tf]:
                self.logger.debug(
                    f"⚪️ {symbol:10} | Not enough bars {tf}: "
                    f"{len(df)}/{min_bars[tf]}"
                )
                return False

            if not required_cols.issubset(df.columns):
                self.logger.warning(f"⚪️ {symbol:10} | Invalid columns on {tf}")
                return False

        return True

    def _calculate_cooldown(self) -> int:
        return 300

    def _check_pd_alignment(self, df: pd.DataFrame, trend: str) -> bool:
        try:
            zones = self.smc.get_pd_zones(df)
            current_zone = zones.get("current_zone")

            if trend == "LONG":
                return current_zone == "DISCOUNT"

            if trend == "SHORT":
                return current_zone == "PREMIUM"

            return False

        except Exception:
            return False


if __name__ == "__main__":
    bot = InstitutionalBot()
    bot.run()

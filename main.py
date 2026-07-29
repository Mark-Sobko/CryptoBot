import logging
import math
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
from core.strategy_journal import StrategyObservationJournal

from engine.filters import MarketFilters
from engine.trend_engine import TrendEngine
from engine.smc_analyzer import SMCAnalyzer
from engine.liquidity import LiquidityEngine
from engine.scoring import ScoringSystem
from engine.indicators import ConfirmationModule
from engine.stats_analyzer import StatsAnalyzer
from engine.news_filter import NewsFilter
from engine.market_regime import MarketRegimeClassifier
from engine.strategies.mean_reversion import MeanReversionStrategy
from engine.strategies.breakout import BreakoutStrategy
from engine.strategies.trend_pullback import TrendPullbackStrategy
from engine.strategies.volatility_expansion import VolatilityExpansionStrategy
from engine.strategy_coordinator import (
    DECISION_CONFLICT,
    DECISION_NO_ACTION,
    DECISION_WATCH_ONLY,
    ReadOnlyStrategyCoordinator,
)
from engine.strategy_execution import (
    ALT_EXECUTION_READY,
    build_single_target_tp_levels,
    build_strategy_execution_plan,
    build_strategy_poi,
    parse_allowed_strategies,
)
from engine.strategy_health import HEALTH_BLOCKED, evaluate_strategy_execution_health


class InstitutionalBot:
    def __init__(self):
        self.audit = TradeLogger()
        self.logger = logging.getLogger("SMC_BOT.MainEngine")

        self._load_runtime_safeguards()
        self._validate_runtime_environment()
        self.strategy_journal = StrategyObservationJournal(
            config.STRATEGY_OBSERVATION_PATH,
            enabled=self.strategy_observation_journal_enabled,
        )

        self.db = TradeDatabase()
        self.ex = ExchangeManager()
        self.notifier = TelegramNotifier()
        self.executor = TradeExecutor(self.ex)

        initial_balance = self.ex.get_total_balance()
        if initial_balance <= 0:
            raise ValueError("Exchange balance is zero or unavailable")

        self.initial_balance = initial_balance
        self.risk_manager = RiskManager(balance=initial_balance)

        self.filters = MarketFilters()
        self.smc = SMCAnalyzer()
        self.liquidity = LiquidityEngine()
        self.scoring = ScoringSystem()
        self.confirmation = ConfirmationModule()
        self.stats_analyzer = StatsAnalyzer(config.DB_PATH, initial_balance=initial_balance)
        self.regime_classifier = MarketRegimeClassifier()
        self.mean_reversion_strategy = MeanReversionStrategy()
        self.breakout_strategy = BreakoutStrategy()
        self.trend_pullback_strategy = TrendPullbackStrategy()
        self.volatility_expansion_strategy = VolatilityExpansionStrategy()
        self.read_only_strategy_coordinator = ReadOnlyStrategyCoordinator()

        self.is_running = True
        self.last_breaker_time: Optional[datetime.datetime] = None

        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

        self.news_filter = NewsFilter()
        self.logger.info("🤖 [INIT] InstitutionalBot initialized successfully")

    def _load_runtime_safeguards(self) -> None:
        global_cfg = config.RISK_MANAGEMENT.get("global", {})
        self.started_at = datetime.datetime.now(datetime.timezone.utc)
        self.max_runtime_minutes = float(global_cfg.get("max_runtime_minutes", 0) or 0)
        self.max_orders_per_run = int(global_cfg.get("max_orders_per_run", 0) or 0)
        self.max_orders_per_cycle = int(global_cfg.get("max_orders_per_cycle", 0) or 0)
        self.max_order_notional_usd = float(global_cfg.get("max_order_notional_usd", 0) or 0)
        self.max_drawdown_limit_pct = float(global_cfg.get("max_drawdown_limit_pct", 0) or 0)
        self.execution_enabled = bool(global_cfg.get("execution_enabled", True))
        self.require_m5_confirmation = bool(global_cfg.get("require_m5_confirmation", True))
        self.require_pd_alignment = bool(global_cfg.get("require_pd_alignment", True))
        self.require_liquidity_target = bool(global_cfg.get("require_liquidity_target", True))
        self.multi_strategy_read_only = bool(global_cfg.get("multi_strategy_read_only", True))
        self.multi_strategy_execution_enabled = bool(
            global_cfg.get("multi_strategy_execution_enabled", False)
        )
        self.multi_strategy_min_rr = float(global_cfg.get("multi_strategy_min_rr", 1.2) or 1.2)
        self.multi_strategy_allowed_strategies = parse_allowed_strategies(
            global_cfg.get("multi_strategy_allowed_strategies", None)
        )
        self.multi_strategy_execution_policy = global_cfg.get(
            "multi_strategy_execution_policy",
            {},
        )
        self.multi_strategy_health_guard_enabled = bool(
            global_cfg.get("multi_strategy_health_guard_enabled", True)
        )
        self.multi_strategy_health_window_minutes = float(
            global_cfg.get("multi_strategy_health_window_minutes", 240.0) or 0.0
        )
        self.multi_strategy_health_max_rejections = int(
            global_cfg.get("multi_strategy_health_max_rejections", 3) or 0
        )
        self.multi_strategy_health_max_executor_failures = int(
            global_cfg.get("multi_strategy_health_max_executor_failures", 1) or 0
        )
        self.strategy_observation_journal_enabled = bool(
            global_cfg.get("strategy_observation_journal", True)
        )
        self.last_drawdown_pct = 0.0
        self.orders_submitted_this_run = 0
        self.orders_submitted_this_cycle = 0
        self.strategy_execution_last_submit: Dict[tuple[str, str], datetime.datetime] = {}
        self.strategy_execution_health_events: list[Dict[str, Any]] = []

    def _validate_runtime_environment(self) -> None:
        global_cfg = config.RISK_MANAGEMENT.get("global", {})
        allow_live = bool(global_cfg.get("allow_live_trading", False))

        if not (config.BYBIT_DEMO or config.BYBIT_TESTNET) and not allow_live:
            raise RuntimeError(
                "SAFEGUARD: main.py refuses live trading unless "
                "RISK_MANAGEMENT['global']['allow_live_trading'] is True"
            )

    def _handle_exit(self, signum, frame) -> None:
        self.logger.info("--- [SYSTEM SHUTDOWN] Stop signal received ---")
        self.is_running = False

    def _runtime_limit_reached(self) -> bool:
        if self.max_runtime_minutes <= 0:
            return False

        elapsed_minutes = (
            datetime.datetime.now(datetime.timezone.utc) - self.started_at
        ).total_seconds() / 60.0

        if elapsed_minutes < self.max_runtime_minutes:
            return False

        self.logger.warning(
            f"🛑 [RUNTIME GUARD] Max runtime reached: "
            f"{elapsed_minutes:.2f}/{self.max_runtime_minutes:.2f} min. "
            "Stopping new work and shutting down gracefully."
        )
        self.is_running = False
        return True

    def _run_order_limit_reached(self) -> bool:
        return (
            self.max_orders_per_run > 0
            and self.orders_submitted_this_run >= self.max_orders_per_run
        )

    def _cycle_order_limit_reached(self) -> bool:
        return (
            self.max_orders_per_cycle > 0
            and self.orders_submitted_this_cycle >= self.max_orders_per_cycle
        )

    def _execution_guard_allows_new_order(self, symbol: str) -> bool:
        if not self.execution_enabled:
            self.logger.info(
                f"🛑 [EXECUTION DISABLED] {symbol} signal observed; order submission skipped"
            )
            return False

        if self._run_order_limit_reached():
            self.logger.warning(
                f"🛑 [ORDER GUARD] {symbol} skipped: max_orders_per_run="
                f"{self.max_orders_per_run} already reached"
            )
            return False

        if self._cycle_order_limit_reached():
            self.logger.warning(
                f"🛑 [ORDER GUARD] {symbol} skipped: max_orders_per_cycle="
                f"{self.max_orders_per_cycle} already reached"
            )
            return False

        return True

    def _cap_qty_by_notional_limit(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        max_notional_usd: float,
        label: str,
    ) -> float:
        if max_notional_usd <= 0:
            return qty

        if not all(math.isfinite(value) and value > 0 for value in [qty, entry_price]):
            return 0.0

        notional = qty * entry_price
        if notional <= max_notional_usd:
            return qty

        capped_qty = max_notional_usd / entry_price
        self.logger.warning(
            f"🛡️ [NOTIONAL GUARD] {symbol} qty reduced by {label}: "
            f"{notional:.2f} -> {max_notional_usd:.2f} USDT"
        )
        return capped_qty

    def _cap_qty_by_notional(self, symbol: str, qty: float, entry_price: float) -> float:
        return self._cap_qty_by_notional_limit(
            symbol,
            qty,
            entry_price,
            self.max_order_notional_usd,
            "max_order_notional_usd",
        )

    def _record_submitted_order(self, symbol: str) -> None:
        self.orders_submitted_this_run += 1
        self.orders_submitted_this_cycle += 1
        self.logger.info(
            f"🧮 [ORDER GUARD] {symbol} submitted. "
            f"run={self.orders_submitted_this_run}/{self.max_orders_per_run or 'unlimited'}, "
            f"cycle={self.orders_submitted_this_cycle}/{self.max_orders_per_cycle or 'unlimited'}"
        )

    def _strategy_execution_cooldown_allows(
        self,
        symbol: str,
        strategy: str,
        cooldown_minutes: float,
    ) -> bool:
        if cooldown_minutes <= 0:
            return True

        last_submit = getattr(self, "strategy_execution_last_submit", None)
        if last_submit is None:
            self.strategy_execution_last_submit = {}
            last_submit = self.strategy_execution_last_submit

        key = (symbol.upper(), strategy.upper())
        last_seen = last_submit.get(key)
        if last_seen is None:
            return True

        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=datetime.timezone.utc)

        elapsed_minutes = (
            datetime.datetime.now(datetime.timezone.utc) - last_seen
        ).total_seconds() / 60.0
        remaining = cooldown_minutes - elapsed_minutes
        if remaining <= 0:
            return True

        self.logger.info(
            f"⏳ [ALT EXEC COOLDOWN] {symbol} | strategy={strategy} | "
            f"remaining={remaining:.1f} min"
        )
        return False

    def _record_strategy_execution_submit(self, symbol: str, strategy: str) -> None:
        last_submit = getattr(self, "strategy_execution_last_submit", None)
        if last_submit is None:
            self.strategy_execution_last_submit = {}

        self.strategy_execution_last_submit[(symbol.upper(), strategy.upper())] = (
            datetime.datetime.now(datetime.timezone.utc)
        )

    def _record_strategy_execution_health_event(
        self,
        symbol: str,
        strategy: str,
        *,
        order_submitted: bool,
        reason: str,
    ) -> None:
        events = getattr(self, "strategy_execution_health_events", None)
        if events is None:
            self.strategy_execution_health_events = []
            events = self.strategy_execution_health_events

        events.append(
            {
                "ts": datetime.datetime.now(datetime.timezone.utc),
                "symbol": str(symbol or "").upper(),
                "strategy": str(strategy or "").upper(),
                "order_submitted": bool(order_submitted),
                "reason": str(reason or ""),
            }
        )

        max_events = 500
        if len(events) > max_events:
            del events[: len(events) - max_events]

    def _strategy_execution_health_allows(
        self,
        symbol: str,
        strategy: str,
    ) -> tuple[bool, str]:
        if not bool(getattr(self, "multi_strategy_health_guard_enabled", True)):
            return True, ""

        health = evaluate_strategy_execution_health(
            getattr(self, "strategy_execution_health_events", []),
            strategy=strategy,
            window_minutes=float(
                getattr(self, "multi_strategy_health_window_minutes", 240.0) or 0.0
            ),
            max_recent_rejections=int(
                getattr(self, "multi_strategy_health_max_rejections", 3) or 0
            ),
            max_executor_failures=int(
                getattr(self, "multi_strategy_health_max_executor_failures", 1) or 0
            ),
        )
        if health.get("status") != HEALTH_BLOCKED:
            return True, ""

        reason = str(health.get("reason", "strategy_health_guard"))
        self.logger.warning(
            f"🧯 [ALT HEALTH BLOCK] {symbol} | strategy={strategy} | "
            f"reason={reason} | recent_events={health.get('recent_events')} | "
            f"executor_failures={health.get('executor_failures')} | "
            f"rejection_streak={health.get('consecutive_unhealthy_rejections')}"
        )
        return False, reason

    @staticmethod
    def _has_effective_liquidity_target(analysis: Dict[str, Any]) -> bool:
        if bool(analysis.get("has_liquidity_target", False)):
            return True

        trend = str(analysis.get("trend", analysis.get("direction", ""))).upper()
        if trend == "LONG":
            return bool(analysis.get("has_eqh", False))
        if trend == "SHORT":
            return bool(analysis.get("has_eql", False) or analysis.get("has_ql", False))
        return False

    def _missing_entry_quality_checks(self, analysis: Dict[str, Any]) -> list[str]:
        missing: list[str] = []

        if self.require_m5_confirmation and not bool(analysis.get("m5_ok", False)):
            missing.append("m5")

        if self.require_pd_alignment and not bool(analysis.get("is_pd_aligned", False)):
            missing.append("pd_alignment")

        if self.require_liquidity_target and not self._has_effective_liquidity_target(analysis):
            missing.append("liquidity_target")

        return missing

    def _analyze_read_only_strategies(
        self,
        symbol: str,
        data: Dict[str, pd.DataFrame],
    ) -> Optional[Dict[str, Any]]:
        analysis_enabled = bool(getattr(self, "multi_strategy_read_only", False))
        execution_enabled = bool(getattr(self, "multi_strategy_execution_enabled", False))
        if not (analysis_enabled or execution_enabled):
            return None

        try:
            regime_result = self.regime_classifier.analyze(data)
            mean_reversion = self.mean_reversion_strategy.analyze(
                symbol=symbol,
                regime_result=regime_result,
                df_15m=data.get("15m"),
                df_5m=data.get("5m"),
            )
            breakout = self.breakout_strategy.analyze(
                symbol=symbol,
                regime_result=regime_result,
                df_15m=data.get("15m"),
                df_5m=data.get("5m"),
            )
            trend_pullback = self.trend_pullback_strategy.analyze(
                symbol=symbol,
                regime_result=regime_result,
                df_1h=data.get("1h"),
                df_15m=data.get("15m"),
                df_5m=data.get("5m"),
            )
            volatility_expansion = self.volatility_expansion_strategy.analyze(
                symbol=symbol,
                regime_result=regime_result,
                df_1h=data.get("1h"),
                df_15m=data.get("15m"),
                df_5m=data.get("5m"),
            )
            decision = self.read_only_strategy_coordinator.decide(
                symbol=symbol,
                regime_result=regime_result,
                strategy_results=[
                    mean_reversion,
                    breakout,
                    trend_pullback,
                    volatility_expansion,
                ],
            )

            result = {
                "symbol": symbol,
                "mean_reversion": mean_reversion,
                "breakout": breakout,
                "trend_pullback": trend_pullback,
                "volatility_expansion": volatility_expansion,
                "decision": decision,
                **regime_result,
            }
            self._log_read_only_strategy_decision(symbol, result)
            self._record_read_only_strategy_observation(symbol, result)
            return result
        except Exception as exc:
            self.logger.warning(
                f"⚠️ [ALT READ-ONLY] {symbol} skipped: {type(exc).__name__}: {str(exc)[:160]}"
            )
            return None

    def _record_read_only_strategy_observation(
        self,
        symbol: str,
        strategy_result: Dict[str, Any],
    ) -> None:
        journal = getattr(self, "strategy_journal", None)
        if journal is None:
            return

        decision = strategy_result.get("decision")
        if not isinstance(decision, dict):
            return

        action = decision.get("decision")
        rejected_count = int(decision.get("rejected_candidate_count", 0) or 0)
        if action == DECISION_NO_ACTION and rejected_count <= 0:
            return

        payload = {
            "source": "ALT_STRATEGY",
            "read_only": True,
            "execution_enabled": bool(getattr(self, "execution_enabled", True)),
            "demo": config.BYBIT_DEMO,
            "testnet": config.BYBIT_TESTNET,
            "regime": strategy_result.get("regime"),
            "regime_confidence": strategy_result.get("confidence"),
            "decision": action,
            "reason": decision.get("reason"),
            "selected_strategy": decision.get("selected_strategy"),
            "side": decision.get("side"),
            "score": decision.get("score"),
            "threshold": decision.get("threshold"),
            "candidate_count": decision.get("candidate_count"),
            "candidate_strategies": decision.get("candidate_strategies", []),
            "candidate_sides": decision.get("candidate_sides", []),
            "rejected_candidate_count": rejected_count,
            "rejected_candidates": decision.get("rejected_candidates", []),
            "plan": decision.get("plan"),
        }
        journal.record("ALT_STRATEGY_DECISION", symbol, payload)

    def _record_strategy_execution_observation(
        self,
        symbol: str,
        execution_plan: Dict[str, Any],
        *,
        order_submitted: bool,
        reason: str = "",
    ) -> None:
        journal = getattr(self, "strategy_journal", None)
        if journal is None:
            self._record_strategy_execution_health_event(
                symbol,
                str(execution_plan.get("strategy", "")),
                order_submitted=order_submitted,
                reason=reason,
            )
            return

        payload = {
            "source": "ALT_STRATEGY",
            "read_only": False,
            "execution_enabled": bool(getattr(self, "execution_enabled", True)),
            "multi_strategy_execution_enabled": bool(
                getattr(self, "multi_strategy_execution_enabled", False)
            ),
            "demo": config.BYBIT_DEMO,
            "testnet": config.BYBIT_TESTNET,
            "order_submitted": bool(order_submitted),
            "reason": reason,
            "strategy": execution_plan.get("strategy"),
            "side": execution_plan.get("side"),
            "score": execution_plan.get("score"),
            "threshold": execution_plan.get("threshold"),
            "order_type": execution_plan.get("order_type"),
            "entry": execution_plan.get("entry"),
            "stop_loss": execution_plan.get("stop_loss"),
            "target": execution_plan.get("target"),
            "rr": execution_plan.get("rr"),
            "policy_min_rr": execution_plan.get("policy_min_rr"),
            "max_notional_usd": execution_plan.get("max_notional_usd"),
            "risk_pct_multiplier": execution_plan.get("risk_pct_multiplier"),
            "cooldown_minutes": execution_plan.get("cooldown_minutes"),
            "max_hold_minutes": execution_plan.get("max_hold_minutes"),
        }
        self._record_strategy_execution_health_event(
            symbol,
            str(execution_plan.get("strategy", "")),
            order_submitted=order_submitted,
            reason=reason,
        )
        journal.record("ALT_STRATEGY_EXECUTION", symbol, payload)

    def _maybe_execute_read_only_strategy(
        self,
        symbol: str,
        strategy_result: Optional[Dict[str, Any]],
        risk_cfg: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not self.multi_strategy_execution_enabled:
            return None

        if not isinstance(strategy_result, dict):
            return None

        decision = strategy_result.get("decision")
        if not isinstance(decision, dict):
            return None

        execution_plan = build_strategy_execution_plan(
            decision,
            allowed_strategies=self.multi_strategy_allowed_strategies,
            min_rr=self.multi_strategy_min_rr,
            strategy_policies=getattr(self, "multi_strategy_execution_policy", {}),
        )

        if execution_plan.get("status") != ALT_EXECUTION_READY:
            reason = str(execution_plan.get("reason", "not_ready"))
            if reason not in {"decision_not_watch_only", "missing_decision"}:
                self.logger.info(
                    f"⏳ [ALT EXEC REJECT] {symbol} | reason={reason} | "
                    f"strategy={execution_plan.get('strategy')}"
                )
            return None

        execution_symbol = str(execution_plan.get("symbol") or symbol)
        side = str(execution_plan["side"])
        entry_price = float(execution_plan["entry"])
        stop_loss = float(execution_plan["stop_loss"])
        target = float(execution_plan["target"])
        score = int(execution_plan["score"])
        order_type = str(execution_plan["order_type"])
        strategy = str(execution_plan["strategy"])
        strategy_max_notional_usd = float(execution_plan.get("max_notional_usd", 0.0) or 0.0)
        risk_pct_multiplier = float(execution_plan.get("risk_pct_multiplier", 1.0) or 1.0)
        cooldown_minutes = float(execution_plan.get("cooldown_minutes", 0.0) or 0.0)
        max_hold_minutes = float(execution_plan.get("max_hold_minutes", 0.0) or 0.0)

        health_allows, health_reason = self._strategy_execution_health_allows(
            execution_symbol,
            strategy,
        )
        if not health_allows:
            self._record_strategy_execution_observation(
                execution_symbol,
                execution_plan,
                order_submitted=False,
                reason=health_reason,
            )
            return None

        if not self._strategy_execution_cooldown_allows(
            execution_symbol,
            strategy,
            cooldown_minutes,
        ):
            self._record_strategy_execution_observation(
                execution_symbol,
                execution_plan,
                order_submitted=False,
                reason="strategy_cooldown",
            )
            return None

        if not self._execution_guard_allows_new_order(execution_symbol):
            return None

        daily_pnl_usd = self.db.get_today_pnl_usd()
        active_positions = self.ex.get_active_positions()

        is_safe, reason = self.risk_manager.check_safety_filters(
            daily_pnl_usd=daily_pnl_usd,
            active_positions=active_positions,
            symbol=execution_symbol,
            exchange_manager=self.ex,
        )
        if not is_safe:
            self.logger.warning(
                f"🛑 [ALT RISK REJECT] {execution_symbol} | "
                f"strategy={strategy} | reason={reason}"
            )
            self._record_strategy_execution_observation(
                execution_symbol,
                execution_plan,
                order_submitted=False,
                reason=f"risk_reject:{reason}",
            )
            return None

        if not self.ex.can_open_new_trade(risk_cfg["max_open_trades"]):
            self.logger.warning(
                f"⚠️ [ALT EXEC REJECT] {execution_symbol} | "
                f"strategy={strategy} | max positions reached: {risk_cfg['max_open_trades']}"
            )
            self._record_strategy_execution_observation(
                execution_symbol,
                execution_plan,
                order_submitted=False,
                reason="max_positions_reached",
            )
            return None

        available_balance = self.ex.get_available_balance()
        qty, corrected_sl = self.risk_manager.calculate_lot_size(
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            available_balance=available_balance,
        )

        if qty <= 0:
            self.logger.warning(
                f"⚠️ [ALT EXEC REJECT] {execution_symbol} | "
                f"strategy={strategy} | qty is zero after risk sizing"
            )
            self._record_strategy_execution_observation(
                execution_symbol,
                execution_plan,
                order_submitted=False,
                reason="zero_qty_after_risk_sizing",
            )
            return None

        if risk_pct_multiplier < 1.0:
            original_qty = qty
            qty *= risk_pct_multiplier
            self.logger.info(
                f"🛡️ [ALT RISK POLICY] {execution_symbol} | strategy={strategy} | "
                f"qty reduced by risk_pct_multiplier={risk_pct_multiplier:.2f}: "
                f"{original_qty:.8f} -> {qty:.8f}"
            )

        qty = self._cap_qty_by_notional(execution_symbol, qty, entry_price)
        qty = self._cap_qty_by_notional_limit(
            execution_symbol,
            qty,
            entry_price,
            strategy_max_notional_usd,
            f"{strategy}_MAX_NOTIONAL_USD",
        )
        if qty <= 0:
            self.logger.warning(
                f"⚠️ [ALT EXEC REJECT] {execution_symbol} | "
                f"strategy={strategy} | qty is zero after notional cap"
            )
            self._record_strategy_execution_observation(
                execution_symbol,
                execution_plan,
                order_submitted=False,
                reason="zero_qty_after_notional_cap",
            )
            return None

        poi = build_strategy_poi(execution_plan)
        tp_levels = build_single_target_tp_levels(execution_plan)
        is_limit_order = order_type == "Limit"

        self.logger.info(
            f"🧭 [ALT EXEC ROUTE] {execution_symbol} | strategy={strategy} | "
            f"side={side} | type={order_type} | entry={entry_price} | "
            f"sl={corrected_sl} | target={target} | rr={execution_plan.get('rr')}"
        )

        entry_result = self.executor.execute_institutional_entry(
            symbol=execution_symbol,
            side=side,
            poi=poi,
            score=score,
            qty=qty,
            sl=corrected_sl,
            risk_pct=float(risk_cfg["risk_per_trade_pct"]) * risk_pct_multiplier,
            order_type=order_type,
            limit_price=entry_price if is_limit_order else None,
            tp_levels_override=tp_levels,
            strategy=strategy,
            source="ALT_STRATEGY",
            max_hold_minutes=max_hold_minutes,
        )

        if not entry_result:
            self.logger.warning(
                f"❌ [ALT EXEC FAILED] {execution_symbol} | strategy={strategy}"
            )
            self._record_strategy_execution_observation(
                execution_symbol,
                execution_plan,
                order_submitted=False,
                reason="executor_failed",
            )
            return None

        self._record_submitted_order(execution_symbol)
        self._record_strategy_execution_submit(execution_symbol, strategy)
        status_text = "ALT PENDING LIMIT" if is_limit_order else "ALT ENTERED"
        self.audit.log_attempt(
            execution_symbol,
            score,
            status_text,
            f"{strategy} confirmed rr={execution_plan.get('rr')}",
        )
        self._record_strategy_execution_observation(
            execution_symbol,
            execution_plan,
            order_submitted=True,
            reason="submitted",
        )

        metrics_data = {
            "adx": getattr(self.filters, "last_adx", 0.0),
            "er": getattr(self.filters, "last_er", 0.0),
            "atr_pct": getattr(self.filters, "last_atr_pct", 0.0),
            "rel_vol": getattr(self.filters, "last_rel_vol", 0.0),
        }
        self.notifier.notify_signal(
            symbol=execution_symbol,
            score=score,
            side=side,
            price=entry_price,
            sl=corrected_sl,
            tp=target,
            metrics=metrics_data,
        )

        return {
            "symbol": execution_symbol,
            "status": "ALT_EXECUTED",
            "side": side,
            "score": score,
            "reason": f"{strategy} submitted",
            "rel_vol": metrics_data["rel_vol"],
        }

    def _log_read_only_strategy_decision(
        self,
        symbol: str,
        strategy_result: Dict[str, Any],
    ) -> None:
        decision = strategy_result.get("decision")
        if not isinstance(decision, dict):
            return

        action = decision.get("decision")
        reason = str(decision.get("reason", ""))
        if action == DECISION_NO_ACTION and reason == "no_strategy_candidate":
            return

        if action == DECISION_WATCH_ONLY:
            plan = decision.get("plan") if isinstance(decision.get("plan"), dict) else {}
            self.logger.info(
                f"🧭 [ALT WATCH READ-ONLY] {symbol} | "
                f"regime={strategy_result.get('regime')} | "
                f"strategy={decision.get('selected_strategy')} | side={decision.get('side')} | "
                f"score={decision.get('score')}/{decision.get('threshold')} | "
                f"entry={plan.get('entry')} | sl={plan.get('stop_loss')} | "
                f"target={plan.get('target')} | rr={plan.get('rr')}"
            )
            return

        if action == DECISION_CONFLICT:
            self.logger.warning(
                f"⚠️ [ALT CONFLICT READ-ONLY] {symbol} | "
                f"regime={strategy_result.get('regime')} | "
                f"sides={decision.get('candidate_sides')} | "
                f"strategies={decision.get('candidate_strategies')}"
            )
            return

        if action == DECISION_NO_ACTION and int(decision.get("rejected_candidate_count", 0) or 0) > 0:
            self.logger.info(
                f"⏳ [ALT REJECT READ-ONLY] {symbol} | "
                f"regime={strategy_result.get('regime')} | reason={reason} | "
                f"rejected={decision.get('rejected_candidates')}"
            )

    @staticmethod
    def _summary_float(value: Any, digits: int = 4) -> Optional[float]:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        return round(numeric, digits)

    @classmethod
    def _build_regime_summary_fields(
        cls,
        strategy_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        metrics = strategy_result.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}
        setup = strategy_result.get("setup")
        if not isinstance(setup, dict):
            setup = {}

        regime = str(strategy_result.get("regime", "") or "")
        native_strategy_by_regime = {
            "RANGE": "mean_reversion",
            "LOW_VOL_COMPRESSION": "breakout",
            "TRENDING": "trend_pullback",
            "CHOP": "volatility_expansion",
        }
        native_strategy = native_strategy_by_regime.get(regime)
        strategy_states: list[Dict[str, Any]] = []
        for key in ("mean_reversion", "breakout", "trend_pullback", "volatility_expansion"):
            result = strategy_result.get(key)
            if not isinstance(result, dict):
                continue
            status = str(result.get("status", "") or "")
            if key != native_strategy and status == "DISABLED":
                continue
            failed_checks = result.get("failed_checks")
            if not isinstance(failed_checks, list):
                failed_checks = []
            strategy_states.append(
                {
                    "strategy": result.get("strategy", key.upper()),
                    "status": status,
                    "reason": result.get("reason"),
                    "failed_checks": failed_checks[:4],
                    "score": result.get("score"),
                    "threshold": result.get("threshold"),
                }
            )

        return {
            "regime": regime,
            "regime_confidence": strategy_result.get("confidence"),
            "trade_posture": strategy_result.get("trade_posture"),
            "regime_reason": strategy_result.get("reason"),
            "setup_status": setup.get("status"),
            "setup_side": setup.get("side"),
            "setup_reason": setup.get("reason"),
            "range_position": cls._summary_float(metrics.get("range_position"), 4),
            "range_width_pct": cls._summary_float(metrics.get("range_width_pct"), 4),
            "adx": cls._summary_float(metrics.get("adx"), 2),
            "atr_pct": cls._summary_float(metrics.get("atr_pct"), 4),
            "relative_volume": cls._summary_float(metrics.get("relative_volume"), 4),
            "strategy_states": strategy_states,
        }

    @staticmethod
    def _decision_blockers(decision: Dict[str, Any]) -> list[str]:
        blockers: list[str] = []
        rejected_candidates = decision.get("rejected_candidates")
        if not isinstance(rejected_candidates, list):
            return blockers

        for candidate in rejected_candidates[:4]:
            if not isinstance(candidate, dict):
                continue
            strategy = str(candidate.get("strategy", "strategy"))
            reason = str(
                candidate.get("coordinator_rejection")
                or candidate.get("reason")
                or candidate.get("status")
                or ""
            )
            if reason:
                blockers.append(f"{strategy}:{reason}")
        return blockers

    @staticmethod
    def _build_read_only_strategy_summary(
        symbol: str,
        strategy_result: Optional[Dict[str, Any]],
        *,
        rel_vol: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(strategy_result, dict):
            return None

        decision = strategy_result.get("decision")
        if not isinstance(decision, dict):
            return None

        action = decision.get("decision")
        regime_fields = InstitutionalBot._build_regime_summary_fields(strategy_result)
        blockers = InstitutionalBot._decision_blockers(decision)

        if action == DECISION_WATCH_ONLY:
            plan = decision.get("plan") if isinstance(decision.get("plan"), dict) else {}
            return {
                **regime_fields,
                "symbol": symbol,
                "status": "ALT_WATCH",
                "side": decision.get("side"),
                "score": decision.get("score"),
                "reason": f"{decision.get('selected_strategy')} read-only candidate",
                "selected_strategy": decision.get("selected_strategy"),
                "blockers": blockers,
                "plan": plan,
                "rel_vol": rel_vol,
            }

        if action == DECISION_CONFLICT:
            strategies = ",".join(str(item) for item in decision.get("candidate_strategies", []))
            return {
                **regime_fields,
                "symbol": symbol,
                "status": "ALT_CONFLICT",
                "reason": f"read-only conflict:{strategies}",
                "candidate_strategies": decision.get("candidate_strategies", []),
                "candidate_sides": decision.get("candidate_sides", []),
                "blockers": blockers,
                "rel_vol": rel_vol,
            }

        if action == DECISION_NO_ACTION and int(decision.get("rejected_candidate_count", 0) or 0) > 0:
            return {
                **regime_fields,
                "symbol": symbol,
                "status": "ALT_REJECT",
                "reason": "read-only candidate rejected by coordinator",
                "blockers": blockers,
                "rel_vol": rel_vol,
            }

        if action == DECISION_NO_ACTION and regime_fields.get("regime") in {
            "RANGE",
            "LOW_VOL_COMPRESSION",
            "CHOP",
        }:
            reason = (
                regime_fields.get("setup_reason")
                or regime_fields.get("regime_reason")
                or decision.get("reason")
                or "no_strategy_candidate"
            )
            return {
                **regime_fields,
                "symbol": symbol,
                "status": "ALT_REGIME",
                "reason": reason,
                "rel_vol": rel_vol,
            }

        return None

    def _drawdown_limit_reached(self, current_balance: float) -> bool:
        if self.max_drawdown_limit_pct <= 0:
            return False

        initial_balance = float(getattr(self, "initial_balance", 0.0) or 0.0)
        if not all(math.isfinite(value) and value > 0 for value in [initial_balance, current_balance]):
            return False

        drawdown_pct = max(0.0, (initial_balance - current_balance) / initial_balance * 100.0)
        self.last_drawdown_pct = drawdown_pct

        if drawdown_pct < self.max_drawdown_limit_pct:
            return False

        self.logger.critical(
            f"🚨 [GLOBAL DRAWDOWN BREAKER] Drawdown limit reached: "
            f"{drawdown_pct:.2f}%/{self.max_drawdown_limit_pct:.2f}%"
        )
        self.is_running = False
        return True

    def run(self) -> None:
        self.logger.info("--- [SYSTEM START] SMC Institutional Alpha v5.0 ---")
        self.notifier.send_message(
            "🚀 <b>Бот запущен.</b> Режим анализа: Multi-Timeframe SMC v5.0."
        )

        while self.is_running:
            if self._runtime_limit_reached():
                break

            try:
                current_balance = self.ex.get_total_balance()
                if current_balance > 0:
                    self.risk_manager.balance = current_balance
                else:
                    current_balance = self.risk_manager.balance

                if self._drawdown_limit_reached(current_balance):
                    self.notifier.send_message(
                        f"🚨 <b>Global Drawdown Limit Reached</b>\n"
                        f"Drawdown: <code>{self.last_drawdown_pct:.2f}%</code>\n"
                        f"Limit: <code>{self.max_drawdown_limit_pct:.2f}%</code>\n"
                        "Бот остановлен через graceful shutdown."
                    )
                    break

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
            if not self.is_running or self._runtime_limit_reached():
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
        self.orders_submitted_this_cycle = 0

        if self._run_order_limit_reached():
            self.logger.info(
                "🛑 [ORDER GUARD] Run order limit already reached. "
                "New-entry scan skipped; active trade management remains enabled."
            )
            return

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

            if self._runtime_limit_reached():
                break

            if self._cycle_order_limit_reached():
                self.logger.info(
                    "🛑 [ORDER GUARD] Cycle order limit reached. "
                    "Stopping new-entry scan for this cycle."
                )
                break

            if symbol in config.BLACKLIST:
                continue
        
            try:
                data = self.ex.fetch_all_timeframes(symbol)
                if not self._validate_market_data(symbol, data):
                    continue

                read_only_strategy_result = self._analyze_read_only_strategies(symbol, data)
                alt_execution_summary = self._maybe_execute_read_only_strategy(
                    symbol,
                    read_only_strategy_result,
                    risk_cfg,
                )
                if alt_execution_summary:
                    summary_list.append(alt_execution_summary)
                    time.sleep(2)
                    continue

                trend = TrendEngine.get_direction(data["1h"], data["15m"])

                if not self.filters.is_market_suitable(data["1h"]):
                    rel_vol = getattr(self.filters, "last_rel_vol", 0.0)
                    read_only_summary = self._build_read_only_strategy_summary(
                        symbol,
                        read_only_strategy_result,
                        rel_vol=rel_vol,
                    )
                    summary_list.append(
                        read_only_summary
                        or {
                            "symbol": symbol,
                            "status": "REJECT",
                            "reason": "Низкий объем/Шум",
                            "rel_vol": rel_vol,
                        }
                    )
                    continue
                
                rel_vol = getattr(self.filters, "last_rel_vol", 0.0)

                if trend == "FLAT":
                    read_only_summary = self._build_read_only_strategy_summary(
                        symbol,
                        read_only_strategy_result,
                        rel_vol=rel_vol,
                    )
                    summary_list.append(
                        read_only_summary
                        or {
                            "symbol": symbol,
                            "status": "FLAT",
                            "rel_vol": rel_vol,
                        }
                    )
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
                    "structure_ok": bool(final_structure.get("structure_ok", False)),
                    "structure_confirmed": bool(final_structure.get("is_confirmed", False)),
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
                    read_only_summary = self._build_read_only_strategy_summary(
                        symbol,
                        read_only_strategy_result,
                        rel_vol=rel_vol,
                    )
                    summary_list.append(
                        read_only_summary
                        or {
                            "symbol": symbol,
                            "status": "REJECT",
                            "reason": f"Недобор баллов ({score}/{risk_cfg['min_score_to_enter']})",
                            "rel_vol": rel_vol,
                        }
                    )
                    time.sleep(2)
                    continue

                missing_entry_checks = self._missing_entry_quality_checks(analysis)
                if missing_entry_checks:
                    reason = ",".join(missing_entry_checks)
                    self.logger.info(
                        f"⏳ [ENTRY QUALITY WAIT] {symbol} | "
                        f"score={score}/{risk_cfg['min_score_to_enter']} | waiting_for={reason}"
                    )
                    summary_list.append({
                        "symbol": symbol,
                        "status": "WAIT_CONFIRMATION",
                        "side": trend,
                        "score": score,
                        "reason": f"waiting_for:{reason}",
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

                qty = self._cap_qty_by_notional(symbol, qty, execution_entry)
                if qty <= 0:
                    self.logger.warning(f"⚠️ {symbol} | Qty is zero after notional cap")
                    continue

                if not self._execution_guard_allows_new_order(symbol):
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
                    self._record_submitted_order(symbol)
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

import logging
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional

import config


class ScoringSystem:
    """
    [INSTITUTIONAL SCORING ENGINE v5.0]
    Многофакторная decision matrix:
    - hard filters
    - BOS / Sweep
    - POI
    - LTF confirmation
    - macro
    - P/D alignment
    - liquidity target
    - session context
    - (NEW) news sentiment alignment
    - (NEW) advanced liquidity context
    """

    def __init__(self):
        self.logger = logging.getLogger("SMC_BOT.ScoringSystem")

        self.weights = getattr(config, "SCORE_WEIGHTS", {
            "trend": 20,
            "structure": 15,
            "zone": 15,
            "confirmation": 15,
            "macro": 15,
        })

        self.modifiers = getattr(config, "SCORE_MODIFIERS", {
            "sweep_bonus": 10,
            "pd_discount_bonus": 5,
            "pd_premium_bonus": 5,
            "session_confluence": 5,
            "high_vol_penalty": -20,
        })

    @staticmethod
    def _normalize_direction(value: Any) -> Optional[str]:
        side = str(value).upper().strip()

        if side in ("BUY", "LONG", "UP"):
            return "LONG"

        if side in ("SELL", "SHORT", "DOWN"):
            return "SHORT"

        return None

    def calculate(self, results: Dict[str, Any]) -> int:
        score = 0
        details = []

        is_sweep = bool(results.get("liquidity_sweep", results.get("sweep_active", False)))
        has_bos = bool(results.get("structure_ok", False))
        poi_confirmed = bool(results.get("poi_ok", False))

        structure_confirmed = has_bos or is_sweep

        trade_direction = self._normalize_direction(
            results.get("direction", results.get("trend"))
        )

        if trade_direction is None:
            self.logger.debug("Scoring rejected: missing/invalid trade direction")
            return 0

        if not structure_confirmed or not poi_confirmed:
            self.logger.debug(
                f"Scoring rejected: structure={structure_confirmed}, poi={poi_confirmed}"
            )
            return 0

        if results.get("trend_ok", False):
            val = self.weights.get("trend", 20)
            score += val
            details.append(f"Trend(+{val})")

        if has_bos:
            val = self.weights.get("structure", 15)
            score += val
            details.append(f"BOS(+{val})")

        if poi_confirmed:
            val = self.weights.get("zone", 15)
            score += val
            details.append(f"POI(+{val})")

        if results.get("m5_ok", results.get("confirmation_ok", False)):
            val = self.weights.get("confirmation", 15)
            score += val
            details.append(f"LTF(+{val})")

        if results.get("macro_ok", False):
            val = self.weights.get("macro", 15)
            score += val
            details.append(f"Macro(+{val})")
        else:
            score -= 10
            details.append("Macro(-10)")

        if is_sweep:
            val = self.modifiers.get("sweep_bonus", 10)
            score += val
            details.append(f"Sweep(+{val})")

        if results.get("is_pd_aligned", False):
            if trade_direction == "LONG":
                val = self.modifiers.get("pd_discount_bonus", 5)
                details.append(f"Discount(+{val})")
            else:
                val = self.modifiers.get("pd_premium_bonus", 5)
                details.append(f"Premium(+{val})")
            score += val

        has_target = bool(results.get("has_liquidity_target", False))

        if not has_target:
            if trade_direction == "LONG" and results.get("has_eqh", False):
                has_target = True
            elif trade_direction == "SHORT" and (
                results.get("has_eql", False) or results.get("has_ql", False)
            ):
                has_target = True

        if has_target:
            score += 5
            details.append("LiqTarget(+5)")

        if is_sweep and has_bos:
            score += 5
            details.append("Sweep+BOS(+5)")

        is_prime, session_type = self._get_temporal_status()

        if is_prime:
            val = self.modifiers.get("session_confluence", 5)
            score += val
            details.append(f"{session_type}(+{val})")
        else:
            if session_type == "WEEKEND":
                score -= 10
                details.append("Weekend(-10)")
            else:
                score -= 5
                details.append("OffSession(-5)")

        if results.get("high_volatility", False):
            val = self.modifiers.get("high_vol_penalty", -20)
            score += val
            details.append(f"HighVol({val})")

        # =====================================================================
        # [INSTITUTIONAL SCALING] Интеграция с расширенными модулями
        # =====================================================================
        
        # 1. Связка с NewsFilter (если передан ключ news_action)
        news_action = str(results.get("news_action", "NONE")).upper()
        if news_action == "BLOCK":
            score = 0
            details.append("NewsBlock(0)")
            self.logger.warning(f"⚠️ Scoring nullified due to Critical News Block.")
        elif news_action == trade_direction:
            score += 10
            details.append("NewsCatalyst(+10)")
        elif news_action not in ("NONE", "BLOCK"):
            score -= 15
            details.append("NewsOpposed(-15)")

        # 2. Связка с LiquidityEngine (если передан ключ liquidity_context)
        liq_context = str(results.get("liquidity_context", "STANDARD")).upper()
        if liq_context == "HIGH_PROBABILITY":
            score += 10
            details.append("LiqHighProb(+10)")
        elif liq_context == "SWEEP_DRIVEN":
            score += 5
            details.append("LiqSweepContext(+5)")
            
        # =====================================================================

        final_score = min(max(int(score), 0), 100)

        self.logger.info(
            f"📊 SCORING REPORT: {final_score}/100 (Grade: {self.get_trade_grade(final_score)}) | {', '.join(details)}"
        )

        return final_score

    def _get_temporal_status(self) -> Tuple[bool, str]:
        now = datetime.now(timezone.utc)

        if now.weekday() >= 5:
            return False, "WEEKEND"

        if 7 <= now.hour <= 18:
            return True, "GOLDEN_WINDOW"

        return False, "NIGHT_ILLIQUID"

    @staticmethod
    def is_executable(score: int, min_threshold: int) -> bool:
        return int(score) >= int(min_threshold)

    @staticmethod
    def get_trade_grade(score: int) -> str:
        """
        Масштабируемый метод для классификации качества сигнала.
        Возвращает буквенный грейд (A+, A, B, C, F) в зависимости от скора.
        Используется для фильтрации алертов в Telegram и аналитики логов.
        """
        if score >= 90:
            return "A+"
        elif score >= 80:
            return "A"
        elif score >= 70:
            return "B"
        elif score >= 50:
            return "C"
        else:
            return "F"

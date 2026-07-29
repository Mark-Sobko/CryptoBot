import html
import logging
import datetime
import time
from collections import Counter
from typing import Optional, Dict, List, Any

import requests

import config


class TelegramNotifier:
    """
    [INSTITUTIONAL MONITORING v4.8]
    Production-safe Telegram notifications.
    Расширен: добавлена отправка форматированной статистики (Stats Reports).
    """

    SAFE_LIMIT = 3900
    TIMEOUT = 12       # ИСПРАВЛЕНИЕ: Увеличили таймаут с 3 до 12 секунд для защиты от микро-лагов сети
    MAX_RETRIES = 3

    def __init__(self):
        self.logger = logging.getLogger("SMC_BOT.Notifier")

        self.token = getattr(config, "TELEGRAM_BOT_TOKEN", "")
        self.chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")

        tg_cfg = getattr(config, "TELEGRAM_CONFIG", {})
        self.alerts = tg_cfg.get("alerts", {}) if isinstance(tg_cfg, dict) else {}
        config_enabled = tg_cfg.get("enabled", True) if isinstance(tg_cfg, dict) else True

        self.enabled = bool(self.token and self.chat_id and config_enabled)
        self.parse_mode = "HTML"
        self.session = requests.Session()

        self.url = (
            f"https://api.telegram.org/bot{self.token}/sendMessage"
            if self.enabled
            else ""
        )

        if self.enabled:
            self.logger.info("🚀 Telegram Monitoring ACTIVE")
        else:
            self.logger.warning("⚠️ Telegram Notifier DISABLED")

    @staticmethod
    def _e(value: Any) -> str:
        return html.escape(str(value))

    def _alert_enabled(self, name: str, default: bool = True) -> bool:
        return bool(self.alerts.get(name, default))

    @staticmethod
    def _fmt_number(value: Any, digits: int = 2, suffix: str = "") -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return ""
        return f"{numeric:.{digits}f}{suffix}"

    def _format_regime_line(self, item: Dict[str, Any]) -> str:
        symbol = self._e(str(item.get("symbol", "")).replace("USDT", ""))
        regime = self._e(item.get("regime", "UNKNOWN"))
        confidence = self._fmt_number(item.get("regime_confidence"), 0, "%")
        setup_status = self._e(item.get("setup_status") or item.get("trade_posture") or "")
        setup_side = self._e(item.get("setup_side") or item.get("side") or "")
        reason = self._e(
            item.get("reason")
            or item.get("setup_reason")
            or item.get("regime_reason")
            or "no_action"
        )
        range_pos = self._fmt_number(item.get("range_position"), 2)
        range_width = self._fmt_number(item.get("range_width_pct"), 2, "%")
        rel_vol = self._fmt_number(item.get("relative_volume", item.get("rel_vol")), 2, "x")

        parts = [f"• <b>#{symbol}</b>: <code>{regime}</code>"]
        if confidence:
            parts.append(confidence)
        if setup_status:
            parts.append(f"setup=<code>{setup_status}</code>")
        if setup_side:
            parts.append(f"side=<code>{setup_side}</code>")

        metrics = []
        if range_pos:
            metrics.append(f"pos={range_pos}")
        if range_width:
            metrics.append(f"width={range_width}")
        if rel_vol:
            metrics.append(f"vol={rel_vol}")
        if metrics:
            parts.append("(" + ", ".join(metrics) + ")")

        blockers = item.get("blockers")
        if isinstance(blockers, list) and blockers:
            reason = self._e(", ".join(str(blocker) for blocker in blockers[:3]))

        parts.append(f"— {reason}")
        return " ".join(parts)

    def _trim(self, text: str) -> str:
        if len(text) <= self.SAFE_LIMIT:
            return text

        suffix = "\n\n⚠️ <i>[ANTI-SPAM] Message truncated due to Telegram API limit.</i>"
        return text[: self.SAFE_LIMIT - len(suffix)] + suffix

    def _execute_send(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.session.post(
                    self.url,
                    json=payload,
                    timeout=self.TIMEOUT,
                )

                if response.status_code == 429:
                    try:
                        retry_after = int(
                            response.json()
                            .get("parameters", {})
                            .get("retry_after", 1)
                        )
                    except Exception:
                        retry_after = 1

                    self.logger.warning(
                        f"⚠️ TG rate limit 429. Retry after {retry_after}s (Attempt {attempt}/{self.MAX_RETRIES})"
                    )
                    time.sleep(min(retry_after, 5))
                    continue

                if response.status_code >= 500:
                    self.logger.warning(
                        f"⚠️ TG server error {response.status_code}. "
                        f"Attempt {attempt}/{self.MAX_RETRIES}"
                    )
                    time.sleep(attempt)
                    continue

                if response.status_code != 200:
                    self.logger.error(
                        f"❌ TG API HTTP Error: {response.status_code} | "
                        f"{response.text[:500]}"
                    )
                    return None

                data = response.json()

                if not data.get("ok", False):
                    self.logger.error(f"❌ TG API Response Error: {data}")
                    return None

                return data

            except requests.exceptions.Timeout:
                # МОДИФИКАЦИЯ: Вместо мгновенного пропуска сообщения, бот сделает паузу и попробует отправить снова
                self.logger.warning(
                    f"⏳ Telegram API timeout on attempt {attempt}/{self.MAX_RETRIES}. Retrying..."
                )
                if attempt < self.MAX_RETRIES:
                    time.sleep(1)
                continue
            except Exception as e:
                self.logger.error(f"🌐 Telegram send error: {e}")
                return None

        self.logger.error("❌ Telegram API timeout. Message skipped after maximum retries.")
        return None

    def send_message(self, text: str, silent: bool = False) -> Optional[Dict[str, Any]]:
        payload = {
            "chat_id": self.chat_id,
            "text": self._trim(text),
            "parse_mode": self.parse_mode,
            "disable_notification": silent,
            "disable_web_page_preview": True,
        }
        return self._execute_send(payload)

    def notify_signal(
        self,
        symbol: str,
        score: int,
        side: str,
        price: float,
        sl: float,
        tp: float,
        metrics: Optional[dict] = None,
    ) -> None:
        if not self._alert_enabled("entry", True):
            return

        clamped_score = max(0, min(100, int(score)))
        filled_blocks = clamped_score // 10
        bar_filled = "🟩" * filled_blocks
        bar_empty = "⬜" * (10 - filled_blocks)

        side_clean = str(side).upper()
        emoji = "🔥 LONG" if side_clean in ("BUY", "LONG", "UP") else "🔻 SHORT"

        risk = abs(price - sl)
        rr = abs(tp - price) / risk if risk > 0 else 0.0

        msg = (
            f"<b>{emoji} | {self._e(symbol)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Confidence Score:</b>\n"
            f"{bar_filled}{bar_empty} <b>{clamped_score}/100</b>\n\n"
            f"🎯 <b>Entry:</b> <code>{price:.5f}</code>\n"
            f"🛡️ <b>Stop Loss:</b> <code>{sl:.5f}</code>\n"
            f"🏁 <b>Take Profit:</b> <code>{tp:.5f}</code>\n\n"
            f"📈 <b>R:R:</b> 1:{rr:.2f}\n"
        )

        if metrics:
            msg += (
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>📈 Smart Money Metrics:</b>\n"
                f"• ADX: <code>{float(metrics.get('adx', 0)):.1f}</code>\n"
                f"• ER: <code>{float(metrics.get('er', 0)):.2f}</code>\n"
                f"• ATR: <code>{float(metrics.get('atr_pct', 0)):.3f}%</code>\n"
                f"• Rel Vol: <code>{float(metrics.get('rel_vol', 0)):.2f}x</code>\n"
            )

        msg += (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 <i>{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</i>"
        )

        self.send_message(msg)

    def notify_market_summary(
        self,
        summary_list: List[Dict[str, Any]],
        equity: float,
    ) -> None:
        if not self._alert_enabled("entry", True):
            return

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )

        msg = (
            f"<b>📋 SMC MARKET SUMMARY</b>\n"
            f"<i>📅 Scan time: {now_str}</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        )

        active_signals = []
        alt_executed = []
        alt_watches = []
        alt_waits = []
        regime_lines = []
        regime_counts: Counter[str] = Counter()
        rejected_coins = []
        flat_coins = []

        for item in summary_list:
            symbol = self._e(str(item.get("symbol", "")).replace("USDT", ""))
            status = str(item.get("status", "UNKNOWN"))
            regime = str(item.get("regime", "") or "")
            if regime:
                regime_counts[regime] += 1
                if status in {"ALT_REGIME", "ALT_REJECT", "ALT_CONFLICT", "ALT_WATCH"}:
                    regime_lines.append(self._format_regime_line(item))

            if status == "SIGNAL":
                active_signals.append(
                    f"• <b>#{symbol}</b>: {self._e(item.get('side', ''))} "
                    f"(Score: {self._e(item.get('score', 0))})"
                )
            elif status == "ALT_EXECUTED":
                alt_executed.append(
                    f"• <b>#{symbol}</b>: {self._e(item.get('side', ''))} "
                    f"{self._e(item.get('reason', 'alternative strategy submitted'))} "
                    f"(Score: {self._e(item.get('score', 0))})"
                )
            elif status == "ALT_WATCH":
                plan = item.get("plan") if isinstance(item.get("plan"), dict) else {}
                rr = self._fmt_number(plan.get("rr"), 2)
                rr_text = f" | R:R <code>{rr}</code>" if rr else ""
                alt_watches.append(
                    f"• <b>#{symbol}</b>: {self._e(item.get('side', ''))} "
                    f"{self._e(item.get('reason', 'read-only candidate'))} "
                    f"(Score: {self._e(item.get('score', 0))}){rr_text}"
                )
            elif status == "ALT_CONFLICT":
                alt_waits.append(
                    f"• <b>#{symbol}</b>: conflict "
                    f"{self._e(item.get('reason', 'strategy conflict'))}"
                )
            elif status == "ALT_REJECT":
                blockers = item.get("blockers")
                details = (
                    ", ".join(str(blocker) for blocker in blockers[:3])
                    if isinstance(blockers, list) and blockers
                    else str(item.get("reason", "candidate rejected"))
                )
                alt_waits.append(f"• <b>#{symbol}</b>: {self._e(details)}")
            elif status == "ALT_REGIME":
                continue
            elif status == "FLAT":
                flat_coins.append(f"<code>{symbol}</code>")
            else:
                reason = self._e(item.get("reason", "noise"))
                rel_vol = float(item.get("rel_vol", 0) or 0)
                rejected_coins.append(
                    f"• <b>{symbol}</b>: {reason} (Vol: {rel_vol:.2f}x)"
                )

        if active_signals:
            msg += "<b>🔥 SIGNALS FOUND:</b>\n" + "\n".join(active_signals) + "\n\n"
        else:
            msg += (
                "<b>🔥 SIGNALS FOUND:</b>\n"
                "• <i>No valid institutional patterns now.</i>\n\n"
            )

        if alt_executed:
            shown = alt_executed[:10]
            msg += "<b>🧭 ALT STRATEGY EXECUTED:</b>\n" + "\n".join(shown)

            if len(alt_executed) > 10:
                msg += f"\n...and {len(alt_executed) - 10} more."

            msg += "\n\n"

        if alt_watches:
            shown = alt_watches[:10]
            msg += "<b>🧭 READ-ONLY ALT WATCH:</b>\n" + "\n".join(shown)

            if len(alt_watches) > 10:
                msg += f"\n...and {len(alt_watches) - 10} more."

            msg += "\n\n"

        if regime_counts:
            counts_text = ", ".join(
                f"<code>{self._e(regime)}</code>: <b>{count}</b>"
                for regime, count in regime_counts.most_common()
            )
            msg += "<b>🧩 REGIME MAP:</b>\n" + counts_text + "\n"
            if regime_lines:
                shown = regime_lines[:12]
                msg += "\n" + "\n".join(shown)
                if len(regime_lines) > 12:
                    msg += f"\n...and {len(regime_lines) - 12} more regime notes."
            msg += "\n\n"

        if alt_waits:
            shown = alt_waits[:10]
            msg += "<b>⏳ ALT STRATEGY WAIT / REJECT:</b>\n" + "\n".join(shown)

            if len(alt_waits) > 10:
                msg += f"\n...and {len(alt_waits) - 10} more."

            msg += "\n\n"

        if rejected_coins:
            shown = rejected_coins[:15]
            msg += "<b>🚫 FILTERED OUT:</b>\n" + "\n".join(shown)

            if len(rejected_coins) > 15:
                msg += f"\n...and {len(rejected_coins) - 15} more."

            msg += "\n\n"

        if flat_coins:
            msg += "<b>💤 FLAT MARKET:</b>\n" + ", ".join(flat_coins) + "\n\n"

        msg += (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Account Equity:</b> <code>{equity:.2f} USDT</code>\n"
            f"🤖 <i>Core status: Watchlist scanning active.</i>"
        )

        self.send_message(msg, silent=True)

    def notify_order_status(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        order_id: str,
    ) -> None:
        if not self._alert_enabled("entry", True):
            return

        msg = (
            f"⚡ <b>ORDER EXECUTED</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"<b>Asset:</b> {self._e(symbol)}\n"
            f"<b>Side:</b> <code>{self._e(str(side).upper())}</code>\n"
            f"<b>Size:</b> <code>{qty}</code>\n"
            f"<b>Avg Price:</b> <code>{price}</code>\n"
            f"<b>Order ID:</b> <code>{self._e(order_id)}</code>\n"
            f"━━━━━━━━━━━━━━━"
        )

        self.send_message(msg)

    def notify_pnl(
        self,
        symbol: str,
        pnl_usd: float,
        pnl_pct: float,
        duration: str = "",
    ) -> None:
        if not self._alert_enabled("exit", True):
            return

        status = "PROFIT 🟢" if pnl_usd > 0 else "LOSS 🔴"
        emoji = "💰" if pnl_usd > 0 else "📉"
        
        # Добавляем время закрытия для контроля синхронизации
        now_str = datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S UTC')

        msg = (
            f"{emoji} <b>TRADE CLOSED: {status}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"<b>Symbol:</b> {self._e(symbol)}\n"
            f"<b>PnL USD:</b> <code>{pnl_usd:+.2f}$</code>\n"
            f"<b>PnL PCT:</b> <code>{pnl_pct:+.2f}%</code>\n"
            f"<b>Duration:</b> <code>{self._e(duration)}</code>\n"
            f"<b>Time:</b> <code>{now_str}</code>\n"
            f"━━━━━━━━━━━━━━━"
        )

        self.send_message(msg)

    def notify_risk_alert(self, reason: str, details: str = "") -> None:
        if not self._alert_enabled("error", True):
            return

        msg = (
            f"🚨 <b>RISK BREAK ALERT</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"<b>REASON:</b> {self._e(reason)}\n"
            f"<b>DETAILS:</b> {self._e(details)}\n\n"
            f"📢 <i>Risk engine adjusted or blocked trading activity.</i>"
        )

        self.send_message(msg, silent=False)

    # =========================================================================
    # [INSTITUTIONAL SCALING] Интеграция с отчетами StatsAnalyzer
    # =========================================================================
    def notify_stats(self, report_chunks: List[str]) -> None:
        """
        Принимает список предварительно отформатированных таблиц (chunks)
        от StatsAnalyzer и отправляет их в Telegram.
        """
        if not self._alert_enabled("exit", True):
            return

        if not report_chunks:
            return

        for chunk in report_chunks:
            # StatsAnalyzer уже возвращает экранированный HTML (html.escape)
            # внутри <pre></pre>, поэтому просто отправляем.
            self.send_message(chunk, silent=True)
            time.sleep(1) # Защита от Flood Control (множество сообщений подряд)

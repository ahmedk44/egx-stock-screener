"""Telegram alert integration for EGX-QuantEngine - visual card UI (Phase 4.1).

Routing:
  - TELEGRAM_USER_CHAT_ID : private bot chat; PRIMARY target for the weekly
    report and all direct signal/exit alerts.
  - TELEGRAM_CHANNEL_ID   : public channel; optional secondary/fallback target.

Messages are rendered as segmented HTML "cards" (buy / exit / daily / weekly).
When credentials are missing the notifier degrades into MOCK mode: cards are
logged locally instead of sent. Network failures never break the trading loop,
and async wrappers make it safe to await from the asyncio session daemon.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from egx_quant.config.stocks_registry import ShariahStatus
from egx_quant.core.shariah_filter import ShariahFilter
from egx_quant.database.models import RiskPlan

logger = logging.getLogger("egx_quant.notifier")

load_dotenv()

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
SEND_TIMEOUT_SECONDS = 10
CARD_SEP = "------------------------------------"

EXIT_REASON_AR: Dict[str, str] = {
    "EXIT_STOP_LOSS": "🛑 ضرب وقف الخسارة",
    "EXIT_TAKE_PROFIT": "🎯 تحقيق الهدف",
    "TRAILING_STOP": "📉 وقف الخسارة المتحرك",
}

SHARIAH_FLAG_AR: Dict[ShariahStatus, str] = {
    ShariahStatus.COMPLIANT: "✅ متوافق (Compliant)",
    ShariahStatus.NON_COMPLIANT: "⛔ غير متوافق (Non-Compliant)",
    ShariahStatus.NEEDS_REVIEW: "⚠️ قيد المراجعة (Needs Review)",
}

SHARIAH_FLAG_SHORT: Dict[ShariahStatus, str] = {
    ShariahStatus.COMPLIANT: "✅ متوافق",
    ShariahStatus.NON_COMPLIANT: "❌ غير متوافق",
    ShariahStatus.NEEDS_REVIEW: "⚠️ يحتاج مراجعة",
}


def translate_exit_reason(reason: str) -> str:
    return EXIT_REASON_AR.get(str(reason).strip().upper(), str(reason))


def clean_ticker(symbol: str) -> str:
    """Strip the .CA exchange suffix for display (ELWA.CA -> ELWA)."""
    s = str(symbol).strip().upper()
    return s[:-3] if s.endswith(".CA") else s


def build_join_markup(trade_id: int, ticker_bare: str) -> Dict[str, Any]:
    """Inline keyboard attaching 'Track Signal' to a channel broadcast."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📥 انضم للصفقة | Track Signal",
                    "callback_data": f"join_trade:{ticker_bare}:{trade_id}",
                }
            ]
        ]
    }


class TelegramNotifier:
    """Card-style HTML notifier with user-chat-first routing."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        user_chat_id: Optional[str] = None,
        channel_id: Optional[str] = None,
    ) -> None:
        import os

        self._token: str = bot_token if bot_token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._user_chat_id: str = (
            user_chat_id if user_chat_id is not None else os.environ.get("TELEGRAM_USER_CHAT_ID", "")
        )
        self._channel_id: str = channel_id if channel_id is not None else os.environ.get("TELEGRAM_CHANNEL_ID", "")
        # Legacy key fallback so pre-4.1 .env files keep working.
        if not self._user_chat_id:
            self._user_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self._shariah = ShariahFilter()
        self.enabled = bool(self._token and (self._user_chat_id or self._channel_id))
        if not self.enabled:
            logger.warning("[NOTIFIER] No Telegram credentials/targets found - running in MOCK mode")

    @property
    def _target_chain(self) -> List[str]:
        """Private-chat targets ONLY. The public channel is served exclusively
        by broadcast_signal() teasers - never by generic sends."""
        chain: List[str] = []
        for target in (self._user_chat_id,):
            t = (target or "").strip()
            if t and t not in chain:
                chain.append(t)
        return chain

    def _post(self, chat_id: str, text: str, parse_mode: str, reply_markup: Optional[Dict[str, Any]] = None) -> requests.Response:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return requests.post(
            TELEGRAM_API_URL.format(token=self._token),
            json=payload,
            timeout=SEND_TIMEOUT_SECONDS,
        )

    def send_to_chat(self, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
        """Deliver to ONE explicit chat (DMs / targeted exits). Mock-aware."""
        if not self.enabled:
            logger.info("[NOTIFIER MOCK -> %s]\n%s", chat_id, text)
            return True
        try:
            resp = self._post(chat_id, text, parse_mode)
            if resp.status_code != 200:
                logger.warning("[NOTIFIER] DM to %s failed (%s): %s", chat_id[:8], resp.status_code, resp.text[:140])
            return resp.status_code == 200
        except requests.exceptions.RequestException as exc:
            logger.error("[NOTIFIER] DM request error (%s...): %s", chat_id[:8], exc)
            return False

    async def send_to_chat_async(self, chat_id: str, text: str) -> bool:
        return await asyncio.to_thread(self.send_to_chat, chat_id, text)

    def broadcast_signal(self, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> bool:
        """STRICT channel teaser broadcast (short card + join button).

        Full buy cards and exit cards must NEVER pass through here.
        """
        if not self.enabled:
            logger.info("[NOTIFIER MOCK BROADCAST]\n%s\nmarkup=%s", text, reply_markup)
            return True
        target = self._channel_id.strip()
        if not target:
            logger.warning("[NOTIFIER] TELEGRAM_CHANNEL_ID unset - channel teaser dropped")
            return False
        try:
            resp = self._post(target, text, "HTML", reply_markup)
            ok = resp.status_code == 200
            if not ok:
                logger.warning("[NOTIFIER] Broadcast failed (%s): %s", resp.status_code, resp.text[:160])
            return ok
        except requests.exceptions.RequestException as exc:
            logger.error("[NOTIFIER] Broadcast request error: %s", exc)
            return False

    async def broadcast_signal_async(self, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> bool:
        return await asyncio.to_thread(self.broadcast_signal, text, reply_markup)

    def format_channel_short_card(self, plan: RiskPlan, trade_id: int) -> List[str]:
        """STRICT channel teaser - the ONLY content allowed on the public channel."""
        bare = clean_ticker(plan.symbol)
        short_flag = SHARIAH_FLAG_SHORT.get(
            self._shariah.get_status(plan.symbol), "⚠️ يحتاج مراجعة"
        )
        t1 = f"{plan.target_1:.2f}" if plan.target_1 else "-"
        lines = [
            f"🚨 <b>إشارة جديدة | {bare}</b>",
            CARD_SEP,
            f"💵 <b>الدخول:</b> {plan.entry_price:.2f} EGP",
            f"🛑 <b>الستوب:</b> {plan.stop_loss:.2f} EGP",
            f"🎯 <b>الهدف الأول:</b> {t1} EGP",
            f"🧠 <b>TQI:</b> {plan.tqi_score:.1f}/10 | {short_flag}",
            CARD_SEP,
            "👇 <b>اضغط الأسفل لمتابعة الصفقة في محفظتك:</b>",
        ]
        return lines

    def format_channel_broadcast(self, plan: RiskPlan, trade_id: int) -> str:
        return "\n".join(self.format_channel_short_card(plan, trade_id))

    def send_text(self, text: str, parse_mode: str = "HTML") -> bool:
        """Direct/private sends. NEVER touches the public channel - the channel
        receives only broadcast_signal() teasers (strict separation)."""
        if not self.enabled:
            logger.info("[NOTIFIER MOCK]\n%s", text)
            return True
        targets = self._target_chain
        last_status, last_body = 0, ""
        for chat_id in targets:
            try:
                resp = self._post(chat_id, text, parse_mode)
            except requests.exceptions.RequestException as exc:
                logger.error("[NOTIFIER] Telegram request error (%s...): %s", chat_id[:6], exc)
                continue
            if resp.status_code == 200:
                return True
            last_status, last_body = resp.status_code, resp.text[:160]
            logger.warning("[NOTIFIER] Send to %s... rejected (%s): %s", chat_id[:6], resp.status_code, resp.text[:120])
        if targets:
            logger.error("[NOTIFIER] All %d target(s) failed. Last: %s %s", len(targets), last_status, last_body)
        return False

    def send_html(self, text: str) -> bool:
        return self.send_text(text, parse_mode="HTML")

    async def send_html_async(self, text: str) -> bool:
        return await asyncio.to_thread(self.send_html, text)

    async def send_text_async(self, text: str) -> bool:
        return await asyncio.to_thread(self.send_text, text)

    def _shariah_flag(self, symbol: str) -> str:
        status = self._shariah.get_status(symbol)
        return SHARIAH_FLAG_AR.get(status, status.value)

    def format_full_dm_card(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        targets: List[float],
        tqi_score: float,
        quantity: Optional[int] = None,
        allocated_cost: Optional[float] = None,
        risk_amount: Optional[float] = None,
    ) -> str:
        """Full private-DM entry card sent the moment a user joins a trade."""
        bare = clean_ticker(symbol)
        flag = SHARIAH_FLAG_AR.get(self._shariah.get_status(symbol), "")
        labels = ("0.618", "100%", "1.618")
        medal = ("🥇", "🥈", "🥉")
        lines = [
            f"🟢 <b>[كارت انضمام للصفقة]</b>",
            CARD_SEP,
            f"🔹 <b>السهم:</b> <code>{bare}</code> {flag}",
            f"🧠 <b>التقييم وجودة الإشارة (TQI):</b> {tqi_score:.1f}/10",
            CARD_SEP,
            f"💵 <b>الدخول:</b> {entry_price:.2f} EGP",
            f"🔴 <b>وقف الخسارة (SL):</b> <b>{stop_loss:.2f}</b> EGP",
            f"🟢 <b>الأهداف (Fibonacci Extensions):</b>",
        ]
        for label, medal_icon, target in zip(labels, medal, targets):
            lines.append(f"  {medal_icon} TP @ {label}: <b>{target:.2f}</b> EGP")
        if quantity is not None:
            lines.append(CARD_SEP)
            lines.append(f"📦 <b>الكمية المقترحة:</b> {quantity} سهم")
        if allocated_cost is not None:
            lines.append(f"💰 <b>التكلفة الإجمالية:</b> {allocated_cost:,.2f} EGP")
        if risk_amount is not None:
            lines.append(f"⚠️ <b>المخاطرة:</b> {risk_amount:,.2f} EGP")
        lines += [CARD_SEP, "<i>تداول فوري (Spot) فقط - شراء ثم بيع</i>", CARD_SEP]
        return "\n".join(lines)

    def format_buy_alert(self, plan: RiskPlan) -> str:
        """Visual BUY signal card."""
        return "\n".join(
            [
                "🟢 <b>[كارت إشارة شراء]</b>",
                CARD_SEP,
                "📊 <b>إشارة دخول جديدة | EGX Quant</b>",
                CARD_SEP,
                f"🔹 <b>السهم:</b> <code>{clean_ticker(plan.symbol)}</code>",
                f"💵 <b>سعر الدخول:</b> {plan.entry_price:.2f} EGP",
                f"🛑 <b>وقف الخسارة:</b> {plan.stop_loss:.2f} EGP",
                f"🎯 <b>جني الأرباح:</b> {plan.take_profit:.2f} EGP",
                CARD_SEP,
                f"📦 <b>الكمية المقترحة:</b> {plan.quantity} سهم",
                f"💰 <b>التكلفة الإجمالية:</b> {plan.allocated_cost:,.2f} EGP",
                f"⚠️ <b>المخاطرة (Risk):</b> {plan.risk_amount:,.2f} EGP",
                f"📐 <b>التوزيع:</b> {plan.allocation_pct_of_portfolio * 100:.1f}% (سقف 20%)",
                f"🕌 <b>الشريعة:</b> {self._shariah_flag(plan.symbol)}",
                CARD_SEP,
            ]
        )

    def format_exit_alert(
        self,
        symbol: str,
        reason: str,
        exit_price: float,
        quantity: int,
        realized_pnl: float,
        pnl_pct: float,
    ) -> str:
        """Visual position-close card with auto-translated exit reason."""
        reason_ar = translate_exit_reason(reason)
        return "\n".join(
            [
                "🔴 <b>[كارت إغلاق صفقة]</b>",
                CARD_SEP,
                f"📌 <b>إغلاق صفقة | <code>{clean_ticker(symbol)}</code></b>",
                CARD_SEP,
                f"🔔 <b>سبب الخروج:</b> {reason_ar}",
                f"💵 <b>سعر الخروج:</b> {exit_price:.2f} EGP",
                f"📦 <b>الكمية:</b> {quantity} سهم",
                f"📊 <b>النتيجة (PnL):</b> {realized_pnl:+,.2f} EGP ({pnl_pct:+.2f}%)",
                CARD_SEP,
            ]
        )

    def format_daily_summary(
        self,
        balance: float,
        available_cash: float,
        open_positions: List[Dict[str, Any]],
    ) -> str:
        """Visual end-of-day portfolio card."""
        lines = [
            "🌙 <b>[كارت نهاية اليوم]</b>",
            CARD_SEP,
            "📊 <b>ملخص المحفظة | Daily Summary</b>",
            CARD_SEP,
            f"💰 <b>رصيد المحفظة:</b> {balance:,.2f} EGP",
            f"💵 <b>الكاش المتاح:</b> {available_cash:,.2f} EGP",
            f"📂 <b>مراكز مفتوحة:</b> {len(open_positions)}",
        ]
        if open_positions:
            lines.append(CARD_SEP)
            for p in open_positions:
                lines.append(
                    f"• <code>{clean_ticker(str(p.get('symbol')))}</code> x{p.get('quantity')} @ "
                    f"{float(p.get('entry_price', 0)):.2f} | 🛑 {float(p.get('stop_loss', 0)):.2f} | "
                    f"🎯 {float(p.get('take_profit', 0)):.2f}"
                )
        lines.append(CARD_SEP)
        return "\n".join(lines)

    async def send_buy_alert_async(self, plan: RiskPlan) -> bool:
        return await self.send_html_async(self.format_buy_alert(plan))

    async def send_exit_alert_async(
        self,
        symbol: str,
        reason: str,
        exit_price: float,
        quantity: int,
        realized_pnl: float,
        pnl_pct: float,
    ) -> bool:
        return await self.send_html_async(
            self.format_exit_alert(symbol, reason, exit_price, quantity, realized_pnl, pnl_pct)
        )

    async def send_daily_summary_async(
        self,
        balance: float,
        available_cash: float,
        open_positions: List[Dict[str, Any]],
    ) -> bool:
        return await self.send_html_async(self.format_daily_summary(balance, available_cash, open_positions))

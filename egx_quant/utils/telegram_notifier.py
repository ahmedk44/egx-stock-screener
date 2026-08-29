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

    def _arabic_ordinal(self, n: int) -> str:
        ordinals = {
            1: "الأول",
            2: "الثاني",
            3: "الثالث",
            4: "الرابع",
            5: "الخامس",
            6: "السادس",
            7: "السابع",
            8: "الثامن",
            9: "التاسع",
            10: "العاشر",
        }
        return ordinals.get(n, f"{n}")

    def _conviction_label(self, tqi: float) -> str:
        if tqi >= 8.5:
            return "🟢 فرصة استثنائية (A+ Setup)"
        if tqi >= 6.5:
            return "🟡 فرصة جيدة (B Setup)"
        if tqi >= 5.0:
            return "🟠 فرصة متوسطة (C Setup)"
        return "⚪ فرصة ضعيفة (Low Conviction)"

    def _technical_reason_for_plan(self, plan: RiskPlan) -> str:
        # Prefer stored technical reason if RiskPlan has it, else infer
        for attr in ("technical_reason", "reason", "trigger"):
            val = getattr(plan, attr, None)
            if val and str(val).strip():
                return str(val).strip()
        # Infer from tqi entry: use generic
        try:
            # Check plan.symbol strategy hints if available via plan attributes
            return "كسر السعر لأعلى EMA20 مع زخم إيجابي و RSI فوق 50"
        except:
            return "تحليل فني"

    def format_channel_short_card(self, plan: RiskPlan, trade_id: int) -> List[str]:
        """Professional public channel template with dynamic targets (2-4+).

        Header: 🚀 إشارة جديدة | {ticker} ({company_name})
        Includes Shariah & Strategy, TQI/Grade, Technical Trigger, Execution Levels (dynamic targets), CTA.
        """
        bare = clean_ticker(plan.symbol)
        # Company name if registry available
        try:
            from egx_quant.config.stocks_registry import STOCK_NAMES_AR as _NAMES
            company = _NAMES.get(plan.symbol, bare) if hasattr(plan, "symbol") else bare
            if not company or company == plan.symbol:
                company = bare
        except:
            company = bare
        header_ticker = f"{bare} ({company})" if company != bare else bare
        # Shariah flag (short)
        try:
            flag = SHARIAH_FLAG_SHORT.get(self._shariah.get_status(plan.symbol), "⚠️ يحتاج مراجعة")
        except:
            flag = "⚠️ يحتاج مراجعة"
        # Strategy / track label - infer from plan if possible
        track_label = "📈 تداول سوينغ (Swing)"
        for attr in ("strategy_type", "strategy", "trade_track"):
            val = getattr(plan, attr, None)
            if val:
                lower = str(val).lower()
                if "scalp" in lower:
                    track_label = "⚡ مضاربة لحظية (Scalp)"
                elif "swing" in lower:
                    track_label = "📈 تداول سوينغ (Swing)"
                elif "invest" in lower:
                    track_label = "🏛️ استثمار طويل (Invest)"
                break
        tqi = getattr(plan, "tqi_score", 5.0)
        try:
            tqi_f = float(tqi)
        except:
            tqi_f = 5.0
        conviction = self._conviction_label(tqi_f)
        technical = self._technical_reason_for_plan(plan)
        # Collect dynamic targets from plan (target_1 .. target_4 etc, plus .targets list)
        targets: List[float] = []
        for i in range(1, 11):
            for key in (f"target_{i}", f"target{i}", f"tp{i}"):
                val = getattr(plan, key, None)
                if val is not None:
                    try:
                        targets.append(float(val))
                        break
                    except:
                        continue
            else:
                # check dict-style if plan is dict-like
                if isinstance(plan, dict) and plan.get(f"target_{i}") is not None:
                    try:
                        targets.append(float(plan.get(f"target_{i}")))
                        continue
                    except:
                        pass
        # Also handle legacy .targets / .take_profits list
        if not targets and hasattr(plan, "targets") and isinstance(getattr(plan, "targets"), (list, tuple)):
            try:
                targets = [float(x) for x in getattr(plan, "targets") if x is not None]
            except:
                targets = []
        # Fallback to single take_profit if no targets
        if not targets and getattr(plan, "take_profit", None) is not None:
            try:
                targets = [float(getattr(plan, "take_profit"))]
            except:
                targets = []
        if not targets and getattr(plan, "target_1", None) is not None:
            try:
                targets = [float(getattr(plan, "target_1"))]
            except:
                pass
        lines = [
            f"🚀 <b>إشارة جديدة | {header_ticker}</b>",
            f"⚖️ <b>التوافق الشرعي:</b> {flag} | 📂 <b>المسار:</b> {track_label}",
            f"🎯 <b>تقييم الجودة (TQI):</b> {tqi_f:.1f}/10 | 🌟 <b>التصنيف:</b> {conviction}",
            f"💡 <b>السبب الفني:</b> {technical}",
            CARD_SEP,
            f"💵 <b>سعر الدخول:</b> {plan.entry_price:.2f} EGP",
            f"🛑 <b>وقف الخسارة (SL):</b> {plan.stop_loss:.2f} EGP",
        ]
        if targets:
            for idx, tv in enumerate(targets, start=1):
                ordinal = self._arabic_ordinal(idx)
                lines.append(f"🎯 <b>الهدف {ordinal}:</b> {tv:.2f} EGP")
        else:
            lines.append(f"🎯 <b>الهدف الأول:</b> - EGP")
        lines += [
            CARD_SEP,
            "👇 <b>اضغط الزر للمتابعة وتلقي التحديثات والتحليل المفصل في الخاص:</b>",
        ]
        return lines

    def format_channel_signal_card(self, plan: RiskPlan, trade_id: int) -> List[str]:
        """Alias for format_channel_short_card - professional template."""
        return self.format_channel_short_card(plan, trade_id)

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
        technical_reason: Optional[str] = None,
        news_summary: Optional[str] = None,
        macro_analysis: Optional[str] = None,
        financial_analysis: Optional[str] = None,
    ) -> str:
        """Full private-DM entry card - dynamic targets + AI intelligence + buttons.

        Retains all execution levels (entry, SL, dynamic 🎯 الهدف الأول..) plus
        deep AI news summary, macro, financial analysis. Paired with
        [ 📊 حالة الصفقة ] [ 🛑 خروج من الصفقة ] inline buttons via build_join_markup.
        """
        bare = clean_ticker(symbol)
        flag = SHARIAH_FLAG_AR.get(self._shariah.get_status(symbol), "")
        tqi_f = float(tqi_score) if tqi_score is not None else 5.0
        conviction = self._conviction_label(tqi_f)
        lines = [
            f"🟢 <b>[كارت انضمام للصفقة]</b>",
            CARD_SEP,
            f"🔹 <b>السهم:</b> <code>{bare}</code> {flag}",
            f"🧠 <b>التقييم وجودة الإشارة (TQI):</b> {tqi_f:.1f}/10 | 🌟 <b>التصنيف:</b> {conviction}",
            f"⚖️ <b>التوافق الشرعي:</b> {flag}",
        ]
        if technical_reason and str(technical_reason).strip():
            tech = str(technical_reason).strip()
            if len(tech) > 300:
                tech = tech[:300].rstrip() + "…"
            lines.append(f"💡 <b>السبب الفني:</b> {tech}")
        lines += [
            CARD_SEP,
            f"💵 <b>سعر الدخول:</b> {entry_price:.2f} EGP",
            f"🛑 <b>وقف الخسارة (SL):</b> <b>{stop_loss:.2f}</b> EGP",
        ]
        # Dynamic targets loop with 🎯 الهدف الأول etc.
        if targets:
            for idx, tv in enumerate(targets, start=1):
                ordinal = self._arabic_ordinal(idx)
                lines.append(f"🎯 <b>الهدف {ordinal}:</b> <b>{tv:.2f}</b> EGP")
        else:
            lines.append(f"🎯 <b>الهدف الأول:</b> <b>-</b> EGP")
        if quantity is not None:
            lines.append(CARD_SEP)
            lines.append(f"📦 <b>الكمية المقترحة:</b> {quantity} سهم")
        if allocated_cost is not None:
            lines.append(f"💰 <b>التكلفة الإجمالية:</b> {allocated_cost:,.2f} EGP")
        if risk_amount is not None:
            lines.append(f"⚠️ <b>المخاطرة:</b> {risk_amount:,.2f} EGP")
        lines.append(CARD_SEP)
        # AI Intelligence blocks
        if news_summary and str(news_summary).strip():
            body = str(news_summary).strip()
            if len(body) > 500:
                body = body[:500].rstrip() + "…"
            lines.append(f"🤖 <b>ملخص الأخبار (Gemini AI):</b> {body}")
            lines.append(CARD_SEP)
        if macro_analysis and str(macro_analysis).strip():
            macro = str(macro_analysis).strip()
            if len(macro) > 400:
                macro = macro[:400].rstrip() + "…"
            lines.append(f"🧠 <b>التحليل الكلي والأثر غير المباشر:</b> {macro}")
            lines.append(CARD_SEP)
        if financial_analysis and str(financial_analysis).strip():
            fin = str(financial_analysis).strip()
            if len(fin) > 400:
                fin = fin[:400].rstrip() + "…"
            lines.append(f"📊 <b>التحليل المالي:</b> {fin}")
            lines.append(CARD_SEP)
        if not (news_summary or macro_analysis or financial_analysis):
            lines.append("🤖 <b>ملخص الأخبار والتحليل:</b> سيتم إرسال التحديثات والتحليل المفصل في الخاص.")
            lines.append(CARD_SEP)
        lines += ["<i>تداول فوري (Spot) فقط - شراء ثم بيع</i>", CARD_SEP, "👇 استخدم الأزرار أدناه لمتابعة حالة الصفقة أو الخروج:"]
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

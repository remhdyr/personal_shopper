"""Telegram notifications and feedback capture.

Sends each qualifying listing as a photo + caption with two inline buttons.
Tapping a button fires a callback that records the user's verdict in the
database, which later feeds the preference-learning few-shot examples.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .db import Database
from .logging_setup import get_logger
from .logistics import LandedCost, Logistics
from .models import VERDICT_ACKS, VERDICT_LABELS, DealAnalysis, Feedback, Listing, Verdict

if TYPE_CHECKING:
    from .triggers import ManualTrigger

log = get_logger(__name__)

_CALLBACK_PREFIX = "fb"


def _callback_data(verdict: Verdict, uid: str) -> str:
    return f"{_CALLBACK_PREFIX}:{verdict.value}:{uid}"


def _build_keyboard(uid: str) -> list[list[InlineKeyboardButton]]:
    """Two-column keyboard: a prominent Interested row, then graded rejections."""

    def btn(verdict: Verdict) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            VERDICT_LABELS[verdict], callback_data=_callback_data(verdict, uid)
        )

    return [
        [btn(Verdict.INTERESTED)],
        [btn(Verdict.TOO_EXPENSIVE), btn(Verdict.POOR_CONDITION)],
        [btn(Verdict.WRONG_TYPE), btn(Verdict.NOT_MY_TASTE)],
    ]


def _money(amount: float, currency: str = "SEK") -> str:
    return f"{amount:,.0f} {currency}".replace(",", " ")


def _format_delivery(cost: LandedCost, currency: str) -> list[str]:
    """Build the delivery/pickup lines shown under the price."""

    bits: list[str] = []
    if cost.shipping_cost is not None:
        if cost.shipping_cost == 0:
            bits.append("\U0001f4e6 shipping included")
        else:
            bits.append(f"\U0001f4e6 ship {_money(cost.shipping_cost, currency)}")
    if cost.pickup_cost is not None:
        dist = f"{cost.distance_km:.0f} km" if cost.distance_km is not None else "?"
        bits.append(f"\U0001f697 pickup ~{_money(cost.pickup_cost, currency)} ({dist})")
    if not bits:
        bits.append("\U0001f4e6 delivery unknown (likely pickup)")

    lines = [" · ".join(bits)]

    tag = " \U0001f4cd nearby" if cost.is_nearby else ""
    if cost.total is not None:
        lines.append(
            f"\U0001f3f7\ufe0f total ~{_money(cost.total, currency)}"
            f" (via {cost.method}){tag}"
        )
    elif tag:
        lines.append(tag.strip())
    return lines


def _format_caption(listing: Listing, analysis: DealAnalysis, cost: LandedCost) -> str:
    price = (
        f"{listing.price:,.0f} {listing.currency}".replace(",", " ")
        if listing.price is not None
        else "price unknown"
    )
    value = (
        f"~{analysis.estimated_value:,.0f} {listing.currency}".replace(",", " ")
        if analysis.estimated_value is not None
        else "n/a"
    )
    lines = [
        f"*{_escape(listing.title)}*",
        f"{_escape(price)}  ·  est\\. value {_escape(value)}",
        f"deal {analysis.deal_score}/100 · fit {analysis.fit_score}/100"
        f" · {_escape(analysis.condition or '?')}",
        *[_escape(line) for line in _format_delivery(cost, listing.currency)],
        "",
        _escape(analysis.summary or listing.description[:280]),
        "",
        f"[View listing]({_escape_url(listing.url)})",
    ]
    return "\n".join(lines)


def _escape(text: str) -> str:
    """Escape text for Telegram MarkdownV2."""

    specials = r"_*[]()~`>#+-=|{}.!"
    return "".join("\\" + ch if ch in specials else ch for ch in text)


def _escape_url(url: str) -> str:
    """Escape a URL for use inside MarkdownV2 link parentheses.

    Only ``)`` and ``\\`` are special there; a raw ``)`` in a marketplace URL
    would otherwise terminate the link early and make Telegram reject the whole
    message with a 400.
    """

    return url.replace("\\", "\\\\").replace(")", "\\)")


class Notifier:
    def __init__(
        self, token: str, chat_id: str, db: Database, logistics: Logistics
    ) -> None:
        self._chat_id = chat_id
        self._db = db
        self._logistics = logistics
        self._trigger: ManualTrigger | None = None
        self._app: Application = Application.builder().token(token).build()
        self._app.add_handler(CommandHandler("start", self._on_start))
        self._app.add_handler(CommandHandler("more", self._on_more))
        self._app.add_handler(
            CallbackQueryHandler(self._on_feedback, pattern=f"^{_CALLBACK_PREFIX}:")
        )

    @property
    def app(self) -> Application:
        return self._app

    def attach_trigger(self, trigger: ManualTrigger) -> None:
        """Wire up the shared 'send more' trigger once the pipeline exists."""

        self._trigger = trigger

    async def register_commands(self) -> None:
        """Register the bot's command menu (the '/' picker in Telegram's UI)."""

        await self._app.bot.set_my_commands(
            [
                BotCommand("more", "Check for new deals right now"),
                BotCommand("start", "Show your chat id"),
            ]
        )

    async def send_listing(self, listing: Listing, analysis: DealAnalysis) -> None:
        cost = self._logistics.estimate(listing, analysis)
        caption = _format_caption(listing, analysis, cost)
        keyboard = InlineKeyboardMarkup(_build_keyboard(listing.uid))
        bot = self._app.bot
        if listing.image_urls:
            await bot.send_photo(
                chat_id=self._chat_id,
                photo=listing.image_urls[0],
                caption=caption,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        else:
            await bot.send_message(
                chat_id=self._chat_id,
                text=caption,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
                disable_web_page_preview=False,
            )
        self._db.mark_notified(listing.uid)

    async def _on_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        log.info("Received /start from chat_id=%s", chat.id)
        await chat.send_message(
            f"Shopper is watching for deals. Your chat id is `{chat.id}`.",
            parse_mode="MarkdownV2",
        )

    async def _on_more(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        if self._trigger is None:
            await chat.send_message("Not ready yet \u2014 try again in a moment.")
            return
        message = await self._trigger.trigger()
        await chat.send_message(message)

    async def _on_feedback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()
        try:
            _prefix, verdict_raw, uid = query.data.split(":", 2)
            verdict = Verdict(verdict_raw)
        except ValueError:
            log.warning("Unparseable callback data: %s", query.data)
            return

        self._db.save_feedback(Feedback(listing_uid=uid, verdict=verdict))
        # The same deal may also be sitting on the dashboard feed; clear it there
        # so the two channels stay in sync.
        self._db.dequeue_from_dashboard(uid)
        log.info("Feedback %s for %s", verdict.value, uid)

        ack = VERDICT_ACKS.get(
            verdict, "Noted \u2014 I'll use that to tune future suggestions."
        )
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            if query.message is not None:
                await query.message.reply_text(ack)
        except Exception as exc:  # noqa: BLE001 - best-effort UI update
            log.debug("Could not update message after feedback: %s", exc)


def build_notifier(
    token: str, chat_id: str, db: Database, logistics: Logistics
) -> Notifier:
    return Notifier(token, chat_id, db, logistics)


# Type alias for a function that yields listings ready to notify.
ListingProducer = Callable[[], list[tuple[Listing, DealAnalysis]]]

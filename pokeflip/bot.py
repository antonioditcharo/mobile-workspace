"""Telegram bot worker - ask the app things from your phone.

Notifications push at you; this pulls. ``/scan`` while you are standing in a
card shop is the point of it.

It long-polls rather than taking a webhook, so it needs no inbound port and
works behind any NAT. Tap-to-act buttons do *not* depend on this worker - those
are plain URL links - so running it is optional.

**Only the configured chat is answered.** A bot token is a URL anyone can
message; without that check, a stranger who guesses your bot's name could read
your portfolio.
"""

from __future__ import annotations

import html
import logging
import threading
import time
from typing import Any, Callable

import httpx

from . import bulk as bulk_mod
from . import digest as digest_mod
from . import orders as orders_mod
from . import portfolio, signals
from .config import Config
from .db import Database

log = logging.getLogger("pokeflip.bot")

POLL_TIMEOUT = 50          # seconds held open by Telegram's long poll
OFFSET_KEY = "telegram_offset"
MAX_MESSAGE = 3800         # Telegram's limit is 4096; leave room for markup


class TelegramBot:
    """Long-polling command handler. Safe to run in a background thread."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.commands: dict[str, Callable[[list[str]], str]] = {
            "start": self._cmd_help,
            "help": self._cmd_help,
            "scan": self._cmd_scan,
            "portfolio": self._cmd_portfolio,
            "listings": self._cmd_listings,
            "orders": self._cmd_orders,
            "risk": self._cmd_risk,
            "bulk": self._cmd_bulk,
            "digest": self._cmd_digest,
            "refresh": self._cmd_refresh,
        }

    # --- lifecycle ------------------------------------------------------

    @property
    def configured(self) -> bool:
        settings = self.config.notify
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    def start(self) -> bool:
        if not self.configured:
            log.info("telegram bot not configured; skipping")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pokeflip-bot",
                                        daemon=True)
        self._thread.start()
        log.info("telegram bot polling for commands")
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.poll_once()
                backoff = 1.0
            except Exception as exc:
                # Never let a transient network failure kill the worker.
                log.warning("telegram poll failed: %s", exc)
                self._stop.wait(min(60.0, backoff))
                backoff = min(60.0, backoff * 2)

    # --- polling --------------------------------------------------------

    def _api(self, method: str, payload: dict[str, Any], timeout: float = 30.0
             ) -> Any:
        token = self.config.notify.telegram_bot_token
        response = httpx.post(f"https://api.telegram.org/bot{token}/{method}",
                              json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data.get("result")

    def poll_once(self) -> int:
        """Fetch and handle one batch of updates. Returns how many were handled."""
        offset = int(self.db.get_setting(OFFSET_KEY, 0) or 0)
        updates = self._api(
            "getUpdates",
            {"offset": offset, "timeout": POLL_TIMEOUT, "allowed_updates": ["message"]},
            timeout=POLL_TIMEOUT + 15,
        ) or []

        handled = 0
        for update in updates:
            # Advance the offset even for updates we ignore, or a message from
            # a stranger would be replayed forever.
            self.db.set_setting(OFFSET_KEY, int(update["update_id"]) + 1)
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = (message.get("text") or "").strip()
            if not text:
                continue
            if chat_id != str(self.config.notify.telegram_chat_id):
                log.warning("ignoring message from unauthorised chat %s", chat_id)
                continue
            self.handle(text, chat_id)
            handled += 1
        return handled

    def handle(self, text: str, chat_id: str) -> str:
        """Run one command and reply. Returns the reply, for tests."""
        parts = text.split()
        name = parts[0].lstrip("/").split("@")[0].lower()
        handler = self.commands.get(name)
        if handler is None:
            reply = (f"Unknown command <b>{html.escape(name)}</b>.\n\n"
                     + self._cmd_help([]))
        else:
            try:
                reply = handler(parts[1:])
            except Exception as exc:
                log.exception("command %s failed", name)
                reply = f"<b>{html.escape(name)}</b> failed: {html.escape(str(exc))}"
        self.send(reply, chat_id)
        return reply

    def send(self, text: str, chat_id: str | None = None) -> None:
        self._api("sendMessage", {
            "chat_id": chat_id or self.config.notify.telegram_chat_id,
            "text": text[:MAX_MESSAGE],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    # --- commands -------------------------------------------------------

    def _cmd_help(self, args: list[str]) -> str:
        return (
            "<b>pokeflip</b>\n"
            "/scan - what to buy and sell right now\n"
            "/portfolio - holdings, value and P&amp;L\n"
            "/listings - live listings needing a decision\n"
            "/orders - open bids and listings\n"
            "/risk - capital concentration\n"
            "/bulk - your bulk tail\n"
            "/digest - today's full action list\n"
            "/refresh - fetch fresh prices now"
        )

    def _cmd_scan(self, args: list[str]) -> str:
        run = signals.generate(self.db, self.config)
        lines: list[str] = []
        if run.sells:
            lines.append("<b>SELL</b>")
            for signal in run.sells[:6]:
                lines.append(
                    f"{html.escape(signal.card_name)} x{signal.quantity} - "
                    f"${signal.price:,.2f} - net ${signal.net_proceeds:,.2f} ea "
                    f"({signal.roi * 100:+.0f}%) - {signal.score:.0f}"
                )
        if run.buys:
            lines.append("\n<b>BUY</b>")
            for signal in run.buys[:6]:
                lines.append(
                    f"{html.escape(signal.card_name)} - bid ${signal.entry_price:,.2f} "
                    f"vs ${signal.price:,.2f} market - "
                    f"${signal.net_profit:,.2f} net ({signal.roi * 100:+.0f}%) - "
                    f"{signal.score:.0f}"
                )
        if not lines:
            return "Nothing clears your thresholds right now. Sitting out is a position."
        lines.append(f"\n<i>Scanned {run.scanned} printings.</i>")
        return "\n".join(lines)

    def _cmd_portfolio(self, args: list[str]) -> str:
        data = portfolio.summary(self.db, self.config)
        realized = data["realized"]
        roi = data["unrealized_roi"]
        return (
            f"<b>Portfolio</b>\n"
            f"{data['cards']} cards in {data['positions']} positions\n"
            f"Cost basis: ${data['cost_basis']:,.2f}\n"
            f"Net if sold now: ${data['net_liquidation']:,.2f}\n"
            f"Unrealised: ${data['unrealized_pnl']:,.2f}"
            + (f" ({roi * 100:+.1f}%)" if roi is not None else "") + "\n"
            f"Realised: ${realized['realized_pnl']:,.2f} over {realized['sales']} sales"
        )

    def _cmd_listings(self, args: list[str]) -> str:
        health = orders_mod.listing_health(self.db, self.config)
        if not health["count"]:
            return "Nothing listed."
        lines = [
            f"<b>{health['count']} listed</b>, ${health['listed_value']:,.2f} at ask, "
            f"{health['stale_count']} stale"
        ]
        for listing in health["needs_action"][:8]:
            suggested = listing.get("suggested_price")
            lines.append(
                f"{listing['verdict'].upper()}: {html.escape(listing['card_name'])} "
                f"${listing['limit_price']:,.2f}"
                + (f" -> ${suggested:,.2f}" if suggested else "")
                + f" ({listing['days_on_market']}d)"
            )
        if not health["needs_action"]:
            lines.append("All priced in line with the market.")
        return "\n".join(lines)

    def _cmd_orders(self, args: list[str]) -> str:
        book = orders_mod.summary(self.db, self.config)
        rows = orders_mod.list_orders(self.db, status="open")
        lines = [
            f"<b>{book['open_buys']} open bids</b> "
            f"(${book['open_buy_value']:,.2f}), "
            f"<b>{book['open_listings']} listings</b> (${book['listed_value']:,.2f})"
        ]
        for order in rows[:10]:
            lines.append(
                f"#{order.id} {order.kind} {html.escape(order.card_name)} "
                f"x{order.quantity} @ ${order.limit_price or 0:,.2f} "
                f"({order.days_open()}d)"
            )
        return "\n".join(lines)

    def _cmd_risk(self, args: list[str]) -> str:
        data = portfolio.concentration(self.db, self.config)
        lines = [f"<b>Capital</b>\nCost basis ${data['total_cost']:,.2f}"]
        if data["free_capital"] is not None:
            lines.append(f"Free capital ${data['free_capital']:,.2f}")
        if data["warnings"]:
            lines.append("")
            lines.extend(f"⚠ {html.escape(w['message'])}" for w in data["warnings"])
        else:
            lines.append("No concentration limits exceeded.")
        return "\n".join(lines)

    def _cmd_bulk(self, args: list[str]) -> str:
        plan = bulk_mod.sell_plan(self.db, self.config)
        return (
            f"<b>Bulk tail</b>\n"
            f"{plan['bulk_card_count']} cards worth ${plan['bulk_net_total']:,.2f} net\n"
            f"List around ${plan['suggested_bulk_ask']:,.2f}\n"
            f"Singles worth listing: ${plan['singles_net_total']:,.2f} net"
        )

    def _cmd_digest(self, args: list[str]) -> str:
        built = digest_mod.build(self.db, self.config, persist_alerts=False)
        if not built.actions:
            return "Nothing clears your thresholds today."
        lines = [f"<b>{html.escape(built.headline)}</b>"]
        for action in built.actions[:10]:
            lines.append(
                f"<b>{action['type'].upper()}</b> {html.escape(action['card'])}\n"
                f"  {html.escape(action['detail'])}"
            )
        return "\n".join(lines)

    def _cmd_refresh(self, args: list[str]) -> str:
        # Imported here: the scheduler owns a bot, so a module-level import
        # would be circular.
        from . import scheduler as scheduler_mod

        result = scheduler_mod.run_refresh_cycle(self.db, self.config, deliver=False)
        return (
            f"Refreshed {result['prices'].get('quotes', 0)} prices.\n"
            f"{result['buys']} buys, {result['sells']} sells, "
            f"{result['alerts']} new alerts, "
            f"{result['listings_needing_action']} listings need a decision."
        )


def run_forever(db: Database, config: Config) -> int:
    """Foreground bot, for ``pokeflip bot``."""
    bot = TelegramBot(db, config)
    if not bot.configured:
        print("Set notify.telegram_bot_token and notify.telegram_chat_id first.")
        return 1
    print("Telegram bot running. Ctrl-C to stop.")
    bot.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        bot.stop()
    return 0

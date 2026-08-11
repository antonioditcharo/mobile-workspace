"""One-tap actions from a notification.

An alert on your phone is only hands-off if you can answer it there. Each
actionable notification carries links like *Bought* or *Snooze*, and each link
is a single-use token that authorises exactly one state change.

Because the token is the authorisation, three rules are non-negotiable:

* it is generated from ``secrets``, not from anything guessable;
* it works **once** - a replayed link reports what already happened rather than
  doing it again;
* it expires.

Anything that would spend money or dispose of cards asks for a number the app
cannot know (what you actually paid), so the token carries a *proposal* and the
tap confirms it at the price the recommendation quoted.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from .config import Config
from .db import Database, iso, parse_ts, utcnow

# What a token is allowed to do. Anything not in here is rejected outright,
# so a malformed or tampered payload cannot reach arbitrary code.
HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {}


class ActionError(RuntimeError):
    """The token is unknown, expired, already used, or cannot be applied."""


@dataclass
class ActionLink:
    token: str
    kind: str
    label: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {"token": self.token, "kind": self.kind, "label": self.label,
                "url": self.url}


def register(kind: str):
    def wrap(func: Callable[..., dict[str, Any]]):
        HANDLERS[kind] = func
        return func
    return wrap


# --- minting ------------------------------------------------------------


def mint(db: Database, config: Config, kind: str, label: str,
         payload: dict[str, Any]) -> ActionLink | None:
    """Create a one-tap link, or ``None`` if the app is not reachable.

    Without ``server.public_base_url`` a button would point nowhere, so no
    token is created rather than shipping a dead link.
    """
    if kind not in HANDLERS:
        raise ActionError(f"unknown action {kind!r}")
    base = (config.server.public_base_url or "").rstrip("/")
    if not base:
        return None

    token = secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(hours=max(1, config.server.action_token_ttl_hours))
    db.execute(
        """
        INSERT INTO action_tokens (token, kind, label, payload, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (token, kind, label, json.dumps(payload), iso(), iso(expires)),
    )
    return ActionLink(token=token, kind=kind, label=label,
                      url=f"{base}/api/act/{token}")


def links_for_alert(db: Database, config: Config, alert: dict[str, Any]
                    ) -> list[ActionLink]:
    """The buttons that belong on one alert.

    Deliberately few. Three choices on a phone screen is a decision; eight is
    a menu you will ignore.
    """
    if not config.notify.actionable or not config.server.public_base_url:
        return []

    kind = alert.get("kind", "")
    payload = alert.get("payload") or {}
    out: list[ActionLink] = []

    def add(action_kind: str, label: str, data: dict[str, Any]) -> None:
        link = mint(db, config, action_kind, label, data)
        if link:
            out.append(link)

    if kind in {"strong_buy", "watch_buy"}:
        signal = payload if payload.get("card_id") else payload.get("metrics", {})
        card_id = alert.get("card_id") or signal.get("card_id")
        if card_id:
            price = (payload.get("entry_price") or payload.get("low")
                     or signal.get("low") or signal.get("price"))
            add("place_buy", "Bid placed", {
                "card_id": card_id,
                "variant": alert.get("variant", "normal"),
                "price": price,
                "quantity": 1,
            })
            add("snooze", "Not interested", {
                "dedupe_key": alert.get("dedupe_key", ""),
                "alert_id": alert.get("id"),
            })

    elif kind in {"strong_sell", "watch_sell"}:
        card_id = alert.get("card_id")
        if card_id:
            add("place_listing", "Listed it", {
                "card_id": card_id,
                "variant": alert.get("variant", "normal"),
                "price": payload.get("price"),
                "quantity": payload.get("quantity", 1),
            })
            add("snooze", "Holding", {
                "dedupe_key": alert.get("dedupe_key", ""),
                "alert_id": alert.get("id"),
            })

    elif kind.startswith("listing_"):
        listing = payload.get("listing") or {}
        order_id = listing.get("id")
        if order_id and listing.get("suggested_price"):
            add("apply_reprice", f"Re-price to ${listing['suggested_price']:,.2f}", {
                "order_id": order_id,
                "price": listing["suggested_price"],
            })
            add("mark_sold", "It sold", {"order_id": order_id,
                                         "price": listing.get("limit_price")})

    if alert.get("id"):
        add("ack", "Dismiss", {"alert_id": alert["id"]})
    return out


# --- executing ----------------------------------------------------------


def execute(db: Database, config: Config, token: str) -> dict[str, Any]:
    """Redeem a token. Raises :class:`ActionError` if it cannot be redeemed."""
    row = db.one("SELECT * FROM action_tokens WHERE token = ?", (token,))
    if row is None:
        raise ActionError("that link is not valid")
    if row["used_at"]:
        # Phones prefetch links and people double-tap. Report the original
        # outcome rather than doing the thing twice.
        try:
            previous = json.loads(row["result"] or "{}")
        except (TypeError, ValueError):
            previous = {}
        return {"status": "already_done", "kind": row["kind"], "label": row["label"],
                "used_at": row["used_at"], "result": previous}
    if parse_ts(row["expires_at"]) < utcnow():
        raise ActionError("that link has expired")

    handler = HANDLERS.get(row["kind"])
    if handler is None:
        raise ActionError(f"unknown action {row['kind']!r}")

    payload = json.loads(row["payload"])
    result = handler(db, config, payload)
    db.execute(
        "UPDATE action_tokens SET used_at = ?, result = ? WHERE token = ?",
        (iso(), json.dumps(result, default=str), token),
    )
    return {"status": "ok", "kind": row["kind"], "label": row["label"],
            "result": result}


def prune(db: Database, keep_days: int = 30) -> int:
    """Drop tokens that expired long enough ago to be uninteresting."""
    cutoff = iso(utcnow() - timedelta(days=keep_days))
    return db.execute("DELETE FROM action_tokens WHERE expires_at < ?", (cutoff,))


# --- handlers -----------------------------------------------------------


@register("place_buy")
def _place_buy(db: Database, config: Config, payload: dict[str, Any]
               ) -> dict[str, Any]:
    """Record that you bid on the card the alert was about."""
    from . import orders

    order_id = orders.create_order(
        db, config, "buy", payload["card_id"],
        quantity=int(payload.get("quantity") or 1),
        limit_price=payload.get("price"),
        variant=payload.get("variant", "normal"),
        notes="placed from a notification",
    )
    return {"order_id": order_id, "message":
            f"Buy order {order_id} recorded. Mark it filled when it arrives."}


@register("place_listing")
def _place_listing(db: Database, config: Config, payload: dict[str, Any]
                   ) -> dict[str, Any]:
    from . import orders

    order_id = orders.create_order(
        db, config, "sell", payload["card_id"],
        quantity=int(payload.get("quantity") or 1),
        limit_price=payload.get("price"),
        variant=payload.get("variant", "normal"),
        notes="listed from a notification",
    )
    return {"order_id": order_id, "message":
            f"Listing {order_id} recorded. Days on market start now."}


@register("apply_reprice")
def _apply_reprice(db: Database, config: Config, payload: dict[str, Any]
                   ) -> dict[str, Any]:
    from . import orders

    result = orders.reprice(db, int(payload["order_id"]), float(payload["price"]),
                            "applied from a notification")
    result["message"] = (
        f"Ask moved from ${result['from']:,.2f} to ${result['to']:,.2f}. "
        "Update the marketplace listing to match."
    )
    return result


@register("mark_sold")
def _mark_sold(db: Database, config: Config, payload: dict[str, Any]
               ) -> dict[str, Any]:
    from . import orders

    result = orders.fill_order(db, config, int(payload["order_id"]),
                               payload.get("price"))
    result["message"] = (
        f"Sold {result['quantity']} for {result.get('net_proceeds', 0):,.2f} net, "
        f"realised {result.get('realized_pnl', 0):+,.2f}."
    )
    return result


@register("snooze")
def _snooze(db: Database, config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    """Stop this exact alert recurring for a while.

    Acknowledging is not enough: the same condition would re-raise it on the
    next cycle. A snooze row keeps the dedupe key suppressed.
    """
    from .alerts import snooze

    days = int(payload.get("days") or 30)
    key = payload.get("dedupe_key") or ""
    if payload.get("alert_id"):
        db.execute("UPDATE alerts SET acknowledged_at = ? WHERE id = ?",
                   (iso(), payload["alert_id"]))
    if key:
        snooze(db, key, days)
    return {"snoozed": key, "days": days,
            "message": f"Muted for {days} days." if key else "Dismissed."}


@register("ack")
def _ack(db: Database, config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    from .alerts import acknowledge

    count = acknowledge(db, payload.get("alert_id"))
    return {"acknowledged": count, "message": "Dismissed."}

"""Delivery of digests and alerts.

Nothing leaves the machine unless a channel is explicitly configured. The
defaults are ``console`` and ``file``, so a fresh install never posts anywhere
until you tell it where.

Push channels (``ntfy``, ``pushover``, ``telegram``) are the ones that reach a
phone, so they behave differently from the rest: they respect a severity floor
and quiet hours, and they carry one-tap buttons when the server is reachable.
An alert you cannot act on from the lock screen is not hands-off.
"""

from __future__ import annotations

import json
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Sequence

import httpx

from .config import Config
from .digest import Digest, render_html, render_markdown

log = logging.getLogger("pokeflip.notify")

# Channels that end up on a phone rather than in a terminal or a file.
PUSH_CHANNELS = {"ntfy", "pushover", "telegram"}
SEVERITY_ORDER = {"info": 0, "warn": 1, "urgent": 2}
# ntfy uses 1-5, Pushover -2..2.
NTFY_PRIORITY = {"info": 3, "warn": 4, "urgent": 5}
PUSHOVER_PRIORITY = {"info": -1, "warn": 0, "urgent": 1}
NTFY_TAGS = {"info": "information_source", "warn": "warning", "urgent": "rotating_light"}
# ntfy renders at most three action buttons.
MAX_ACTIONS = 3


class DeliveryError(RuntimeError):
    pass


def _local_hour(config: Config) -> int:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(config.timezone)).hour
    except Exception:
        # An unknown timezone should not stop a notification going out.
        return datetime.now().hour


def in_quiet_hours(config: Config, hour: int | None = None) -> bool:
    settings = config.notify
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    if start == end:
        return False
    hour = _local_hour(config) if hour is None else hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end   # window wraps past midnight


def should_push(config: Config, alert: dict[str, Any], hour: int | None = None) -> bool:
    """Whether one alert is worth waking a phone for."""
    severity = alert.get("severity", "info")
    floor = SEVERITY_ORDER.get(config.notify.push_min_severity, 1)
    if SEVERITY_ORDER.get(severity, 0) < floor:
        return False
    if in_quiet_hours(config, hour):
        return severity == "urgent" and config.notify.quiet_hours_allow_urgent
    return True


def deliver_digest(config: Config, digest: Digest, paths: dict[str, str] | None = None
                   ) -> list[dict[str, Any]]:
    """Send the digest to every configured channel; never raise on one failure."""
    results: list[dict[str, Any]] = []
    subject = f"pokeflip {digest.kind} digest - {digest.headline}"
    markdown = render_markdown(digest)

    for channel in config.notify.channels:
        try:
            if channel == "console":
                print(markdown)
                results.append({"channel": channel, "ok": True})
            elif channel == "file":
                results.append({"channel": channel, "ok": True,
                                "paths": paths or {}, "note": "written by digest.save"})
            elif channel == "webhook":
                _post_json(config.notify.webhook_url, digest.to_dict())
                results.append({"channel": channel, "ok": True})
            elif channel == "slack":
                _post_json(config.notify.slack_webhook_url,
                           {"text": _chat_text(digest, subject)})
                results.append({"channel": channel, "ok": True})
            elif channel == "discord":
                _post_json(config.notify.discord_webhook_url,
                           {"content": _chat_text(digest, subject)[:1900]})
                results.append({"channel": channel, "ok": True})
            elif channel == "email":
                _send_email(config, subject, markdown, render_html(digest))
                results.append({"channel": channel, "ok": True})
            elif channel in PUSH_CHANNELS:
                # The digest is a summary, not an interrupt: it goes out as one
                # notification at normal priority with no buttons.
                summary = {
                    "title": subject,
                    "body": _push_digest_body(digest),
                    "severity": "info",
                }
                {"ntfy": _send_ntfy, "pushover": _send_pushover,
                 "telegram": _send_telegram}[channel](config, summary, [])
                results.append({"channel": channel, "ok": True})
            else:
                results.append({"channel": channel, "ok": False,
                                "error": "unknown channel"})
        except Exception as exc:
            log.warning("delivery to %s failed: %s", channel, exc)
            results.append({"channel": channel, "ok": False, "error": str(exc)})
    return results


def deliver_alerts(config: Config, alerts: Sequence[dict[str, Any]],
                   db: Any = None) -> list[dict[str, Any]]:
    """Push alerts out as they fire, if realtime alerting is on.

    ``db`` is optional but is what makes buttons possible: action tokens are
    minted per alert. Without it the notifications are read-only.
    """
    if not alerts or not config.notify.realtime_alerts:
        return []

    lines = [
        f"[{a.get('severity', 'info').upper()}] {a.get('title', '')}"
        + (f" - {a['body']}" if a.get("body") else "")
        for a in alerts
    ]
    text = "pokeflip alerts\n" + "\n".join(f"* {line}" for line in lines)
    results: list[dict[str, Any]] = []

    # Phone channels are per-alert (each gets its own buttons) and filtered;
    # everything else gets the batch, unfiltered.
    pushable = [a for a in alerts if should_push(config, a)]
    actions_by_alert = _actions_for(config, db, pushable)

    for channel in config.notify.channels:
        try:
            if channel in PUSH_CHANNELS:
                if not pushable:
                    results.append({"channel": channel, "ok": True, "sent": 0,
                                    "note": "nothing met the push threshold"})
                    continue
                sender = {"ntfy": _send_ntfy, "pushover": _send_pushover,
                          "telegram": _send_telegram}[channel]
                sent = 0
                for alert in pushable:
                    sender(config, alert, actions_by_alert.get(id(alert), []))
                    sent += 1
                results.append({"channel": channel, "ok": True, "sent": sent,
                                "suppressed": len(alerts) - sent})
            elif channel == "console":
                print(text)
                results.append({"channel": channel, "ok": True})
            elif channel == "webhook":
                _post_json(config.notify.webhook_url, {"alerts": list(alerts)})
                results.append({"channel": channel, "ok": True})
            elif channel == "slack":
                _post_json(config.notify.slack_webhook_url, {"text": text})
                results.append({"channel": channel, "ok": True})
            elif channel == "discord":
                _post_json(config.notify.discord_webhook_url, {"content": text[:1900]})
                results.append({"channel": channel, "ok": True})
            elif channel == "email":
                _send_email(config, f"pokeflip: {len(alerts)} alert(s)", text, None)
                results.append({"channel": channel, "ok": True})
            elif channel == "file":
                path = Path(config.notify.report_dir) / "alerts.log"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a") as fh:
                    for alert in alerts:
                        fh.write(json.dumps(alert, default=str) + "\n")
                results.append({"channel": channel, "ok": True, "path": str(path)})
        except Exception as exc:
            log.warning("alert delivery to %s failed: %s", channel, exc)
            results.append({"channel": channel, "ok": False, "error": str(exc)})
    return results


def _actions_for(config: Config, db: Any, alerts: Sequence[dict[str, Any]]
                 ) -> dict[int, list[Any]]:
    """Mint one-tap links per alert, once, shared across every push channel."""
    if db is None or not config.notify.actionable:
        return {}
    from .actions import links_for_alert

    out: dict[int, list[Any]] = {}
    for alert in alerts:
        try:
            out[id(alert)] = links_for_alert(db, config, alert)
        except Exception as exc:
            # A button that cannot be minted must not stop the alert itself.
            log.warning("could not build actions for alert %s: %s",
                        alert.get("id"), exc)
    return out


# --- phone push ---------------------------------------------------------


def _send_ntfy(config: Config, alert: dict[str, Any], actions: Sequence[Any]) -> None:
    settings = config.notify
    if not settings.ntfy_topic:
        raise DeliveryError("notify.ntfy_topic is not set")

    severity = alert.get("severity", "info")
    payload: dict[str, Any] = {
        "topic": settings.ntfy_topic,
        "title": alert.get("title", "pokeflip"),
        "message": alert.get("body") or alert.get("title", ""),
        "priority": NTFY_PRIORITY.get(severity, 3),
        "tags": [NTFY_TAGS.get(severity, "information_source")],
    }
    if actions:
        payload["actions"] = [
            {"action": "http", "label": link.label, "url": link.url,
             "method": "POST", "clear": True}
            for link in actions[:MAX_ACTIONS]
        ]
    headers = {}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"

    response = httpx.post(settings.ntfy_server.rstrip("/"), json=payload,
                          headers=headers, timeout=20.0)
    response.raise_for_status()


def _send_pushover(config: Config, alert: dict[str, Any],
                   actions: Sequence[Any]) -> None:
    settings = config.notify
    if not (settings.pushover_token and settings.pushover_user):
        raise DeliveryError("notify.pushover_token and notify.pushover_user are required")

    data = {
        "token": settings.pushover_token,
        "user": settings.pushover_user,
        "title": alert.get("title", "pokeflip"),
        "message": alert.get("body") or alert.get("title", ""),
        "priority": PUSHOVER_PRIORITY.get(alert.get("severity", "info"), 0),
    }
    # Pushover allows a single supplementary link, so the first action wins.
    if actions:
        data["url"] = actions[0].url
        data["url_title"] = actions[0].label

    response = httpx.post("https://api.pushover.net/1/messages.json", data=data,
                          timeout=20.0)
    response.raise_for_status()


def _send_telegram(config: Config, alert: dict[str, Any],
                   actions: Sequence[Any]) -> None:
    settings = config.notify
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        raise DeliveryError(
            "notify.telegram_bot_token and notify.telegram_chat_id are required")

    body = f"*{_escape_md(alert.get('title', 'pokeflip'))}*"
    if alert.get("body"):
        body += f"\n{_escape_md(alert['body'])}"

    payload: dict[str, Any] = {
        "chat_id": settings.telegram_chat_id,
        "text": body,
        "parse_mode": "MarkdownV2",
    }
    if actions:
        # URL buttons open the action link directly, so taps work without the
        # bot worker running.
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": link.label, "url": link.url}]
                                for link in actions[:MAX_ACTIONS]]
        }
    _telegram_call(settings.telegram_bot_token, "sendMessage", payload)


def _telegram_call(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(f"https://api.telegram.org/bot{token}/{method}",
                          json=payload, timeout=25.0)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise DeliveryError(f"telegram {method} failed: {data.get('description')}")
    return data.get("result", {})


def _escape_md(text: str) -> str:
    """Escape for Telegram MarkdownV2, which is strict about punctuation."""
    for char in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(char, f"\\{char}")
    return text


def _chat_text(digest: Digest, subject: str) -> str:
    """A short chat-friendly rendering - the action list, nothing else."""
    lines = [f"*{subject}*"]
    if not digest.actions:
        lines.append("_Nothing clears your thresholds today._")
    for action in digest.actions[:10]:
        lines.append(
            f"- *{action['type'].upper()}* {action['card']} — {action['detail']}"
        )
    p = digest.portfolio or {}
    if p.get("net_liquidation") is not None:
        lines.append(
            f"_Portfolio: ${p['net_liquidation']:,.2f} net, "
            f"{p.get('unrealized_pnl', 0):+,.2f} unrealised_"
        )
    return "\n".join(lines)


def _push_digest_body(digest: Digest, limit: int = 6) -> str:
    """The action list, short enough to read on a lock screen."""
    if not digest.actions:
        return "Nothing clears your thresholds today."
    lines = [f"{a['type'].upper()}: {a['card']}" for a in digest.actions[:limit]]
    remaining = len(digest.actions) - len(lines)
    if remaining > 0:
        lines.append(f"...and {remaining} more")
    return "\n".join(lines)


def _post_json(url: str, payload: dict[str, Any]) -> None:
    if not url:
        raise DeliveryError("no URL configured for this channel")
    response = httpx.post(url, json=payload, timeout=20.0)
    response.raise_for_status()


def _send_email(config: Config, subject: str, text: str, html: str | None) -> None:
    settings = config.notify
    if not settings.email_to:
        raise DeliveryError("notify.email_to is not set")
    if not settings.smtp_host:
        raise DeliveryError("notify.smtp_host is not set")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.email_from
    message["To"] = settings.email_to
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        if settings.smtp_starttls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)

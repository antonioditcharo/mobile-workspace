"""Delivery of digests and alerts.

Nothing leaves the machine unless a channel is explicitly configured. The
defaults are ``console`` and ``file``, so a fresh install never posts anywhere
until you tell it where.
"""

from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Sequence

import httpx

from .config import Config
from .digest import Digest, render_html, render_markdown

log = logging.getLogger("pokeflip.notify")


class DeliveryError(RuntimeError):
    pass


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
            else:
                results.append({"channel": channel, "ok": False,
                                "error": "unknown channel"})
        except Exception as exc:
            log.warning("delivery to %s failed: %s", channel, exc)
            results.append({"channel": channel, "ok": False, "error": str(exc)})
    return results


def deliver_alerts(config: Config, alerts: Sequence[dict[str, Any]]
                   ) -> list[dict[str, Any]]:
    """Push alerts out as they fire, if realtime alerting is on."""
    if not alerts or not config.notify.realtime_alerts:
        return []

    lines = [
        f"[{a.get('severity', 'info').upper()}] {a.get('title', '')}"
        + (f" - {a['body']}" if a.get("body") else "")
        for a in alerts
    ]
    text = "pokeflip alerts\n" + "\n".join(f"* {line}" for line in lines)
    results: list[dict[str, Any]] = []

    for channel in config.notify.channels:
        try:
            if channel == "console":
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

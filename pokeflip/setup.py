"""Interactive setup and a health check for your own configuration.

Two jobs:

* ``pokeflip setup`` asks the handful of questions the app cannot answer for
  itself and writes ``config.json``. It generates the API token for you rather
  than leaving a placeholder that gets shipped as-is.
* ``pokeflip doctor`` checks what you actually have - can it reach the price
  source, is anything tracked, is delivery wired up, is the server exposed
  without a token - and tells you the fix for each thing it finds.

Both are safe to re-run.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .db import Database, utcnow

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "fix": self.fix}


# --- doctor -------------------------------------------------------------


def diagnose(db: Database, config: Config, check_network: bool = True
             ) -> list[Check]:
    """Everything worth knowing before trusting this thing unattended."""
    checks: list[Check] = []

    checks.append(_check_database(db))
    checks.extend(_check_tracking(db, config))
    if check_network:
        checks.extend(_check_providers(db, config))
    checks.extend(_check_economics(config))
    checks.extend(_check_delivery(config))
    checks.extend(_check_exposure(config))
    checks.append(_check_schedule(config))
    return checks


def _check_database(db: Database) -> Check:
    try:
        counts = db.one(
            """
            SELECT (SELECT COUNT(*) FROM cards) AS cards,
                   (SELECT COUNT(*) FROM price_history) AS prices,
                   (SELECT COUNT(*) FROM holdings WHERE status='open') AS lots
            """
        )
    except Exception as exc:
        return Check("database", FAIL, f"cannot read {db.path}: {exc}",
                     "Check the path in config.json and that it is writable.")
    return Check(
        "database", PASS,
        f"{db.path} - {counts['cards']} cards, {counts['prices']} price points, "
        f"{counts['lots']} open lots",
    )


def _check_tracking(db: Database, config: Config) -> list[Check]:
    out: list[Check] = []
    watched = db.one("SELECT COUNT(*) AS n FROM watchlist WHERE active=1")["n"]
    held = db.one("SELECT COUNT(*) AS n FROM holdings WHERE status='open'")["n"]
    sets = len(config.tracked_sets)

    if watched or held or sets:
        out.append(Check(
            "tracking", PASS,
            f"{held} holdings, {watched} watchlist entries, {sets} tracked sets",
        ))
    else:
        out.append(Check(
            "tracking", FAIL, "nothing is being tracked, so nothing will be priced",
            "Add inventory with `pokeflip hold add`, cards to watch with "
            "`pokeflip watch add`, or a set via tracked_sets in config.json.",
        ))

    # `on` is a reserved word in SQL, so the alias has to be something else.
    latest = db.one("SELECT MAX(captured_on) AS last_day FROM price_history")
    if latest and latest["last_day"]:
        from datetime import date

        last_day = latest["last_day"]
        age = (utcnow().date() - date.fromisoformat(last_day)).days
        if age <= 2:
            out.append(Check("price freshness", PASS,
                             f"last captured {last_day} ({age}d ago)"))
        else:
            out.append(Check(
                "price freshness", WARN,
                f"last captured {last_day} ({age}d ago)",
                "Run `pokeflip refresh`, or check that the scheduler is running.",
            ))
    else:
        out.append(Check(
            "price freshness", WARN, "no prices captured yet",
            "Run `pokeflip refresh` once something is tracked.",
        ))
    return out


def _check_providers(db: Database, config: Config) -> list[Check]:
    """Call the sources for real.

    The catalog source and the price source are not always the same object -
    eBay has no card database - so checking one and reporting on the other
    would happily pass a broken setup.
    """
    from .providers import provides_catalog

    out = [_check_catalog_source(config)]
    if not provides_catalog(config):
        out.extend(_check_price_source(db, config))
    return out


def _check_catalog_source(config: Config) -> Check:
    from .providers import ProviderError, build_catalog_provider

    if config.provider.name == "fixture":
        return Check(
            "catalog source", WARN, "using the offline fixture provider",
            "Set provider.name to 'pokemontcg' (or 'ebay') for real prices.",
        )

    provider = None
    try:
        provider = build_catalog_provider(config)
        name = getattr(provider, "name", "catalog")
        sets = provider.list_sets()
        if not sets:
            return Check("catalog source", WARN,
                         f"{name} answered but returned no sets",
                         "The source may be having a bad day; retry later.")
        return Check("catalog source", PASS,
                     f"{name} reachable - {len(sets)} sets visible")
    except ProviderError as exc:
        return Check(
            "catalog source", FAIL, f"unreachable: {exc}",
            "Check network access. If the provider is blocked where this runs, "
            "set provider.name to 'fixture' to work offline.",
        )
    except Exception as exc:
        return Check("catalog source", FAIL, f"failed: {exc}", "")
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass


def _check_price_source(db: Database, config: Config) -> list[Check]:
    """A price source with no catalog of its own - currently only eBay."""
    from .providers import build_provider

    out: list[Check] = []
    provider = None
    try:
        provider = build_provider(config)
        checker = getattr(provider, "check_credentials", None)
        if checker is None:
            return [Check("price source", WARN,
                          f"{config.provider.name} has no self-check", "")]

        report = checker()
        if report["problems"]:
            out.append(Check(
                "price source", FAIL,
                f"{config.provider.name} ({report['environment']}, "
                f"{report['marketplace']}): {report['problems'][0]}",
                "; ".join(report["problems"][1:]) or "",
            ))
        elif report["sold_data"]:
            out.append(Check(
                "price source", PASS,
                f"eBay authenticated with sold data - real sale prices and "
                f"velocity ({report.get('sold_seen', 0)} sales on a test query)",
            ))
        else:
            out.append(Check(
                "price source", WARN,
                "eBay authenticated, but without sold data",
                "Prices come from the lower quartile of asking prices and "
                "velocity is unknown. Apply for Marketplace Insights access to "
                "get real sale prices.",
            ))
        for note in report.get("notes", []):
            out.append(Check("price source note", WARN, note, ""))
    except Exception as exc:
        out.append(Check("price source", FAIL,
                         f"{config.provider.name} check failed: {exc}", ""))
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass

    # eBay searches by card name, so an empty catalog means it has nothing to
    # look up no matter how good the credentials are.
    cards = db.one("SELECT COUNT(*) AS n FROM cards")["n"]
    if not cards:
        out.append(Check(
            "catalog contents", FAIL,
            "no cards known, so eBay has nothing to search for",
            "Populate the catalog first: `pokeflip sync --set sv3pt5`.",
        ))
    else:
        out.append(Check("catalog contents", PASS, f"{cards} cards known"))
    return out


def _check_economics(config: Config) -> list[Check]:
    out: list[Check] = []
    fees = config.fees
    total_pct = fees.commission_pct + fees.payment_pct
    if total_pct <= 0:
        out.append(Check(
            "fees", FAIL, "no selling fees configured, so every flip looks profitable",
            "Set fees.commission_pct to your marketplace's rate "
            "(TCGplayer ~0.1025, eBay ~0.1325).",
        ))
    elif fees.shipping_cost <= 0:
        out.append(Check(
            "fees", WARN,
            f"{total_pct * 100:.2f}% in fees but no shipping cost",
            "Set fees.shipping_cost to what a stamp, sleeve and toploader "
            "actually cost you.",
        ))
    else:
        out.append(Check(
            "fees", PASS,
            f"{total_pct * 100:.2f}% + ${fees.payment_flat:.2f} per order, "
            f"${fees.shipping_cost:.2f} shipping",
        ))

    if config.capital.bankroll <= 0:
        out.append(Check(
            "bankroll", WARN, "not set, so free capital is not tracked",
            "Set capital.bankroll to enable free-capital and over-commitment "
            "warnings.",
        ))
    else:
        out.append(Check("bankroll", PASS, f"${config.capital.bankroll:,.2f}"))
    return out


def _check_delivery(config: Config) -> list[Check]:
    settings = config.notify
    out: list[Check] = []
    channels = list(settings.channels)

    if not channels:
        return [Check("delivery", FAIL, "no channels configured",
                      "Set notify.channels, e.g. [\"file\", \"ntfy\"].")]

    missing: list[str] = []
    for channel in channels:
        needed = {
            "ntfy": [("ntfy_topic", settings.ntfy_topic)],
            "pushover": [("pushover_token", settings.pushover_token),
                         ("pushover_user", settings.pushover_user)],
            "telegram": [("telegram_bot_token", settings.telegram_bot_token),
                         ("telegram_chat_id", settings.telegram_chat_id)],
            "slack": [("slack_webhook_url", settings.slack_webhook_url)],
            "discord": [("discord_webhook_url", settings.discord_webhook_url)],
            "webhook": [("webhook_url", settings.webhook_url)],
            "email": [("email_to", settings.email_to),
                      ("smtp_host", settings.smtp_host)],
        }.get(channel, [])
        missing.extend(f"notify.{key}" for key, value in needed if not value)

    if missing:
        out.append(Check(
            "delivery", FAIL,
            f"channels {channels} but missing {', '.join(sorted(set(missing)))}",
            "Fill those in, then check with `pokeflip notify test`.",
        ))
    else:
        out.append(Check("delivery", PASS, f"channels {channels} are configured"))

    phone = [c for c in channels if c in {"ntfy", "pushover", "telegram"}]
    if not phone:
        out.append(Check(
            "phone push", WARN, "no channel that reaches a phone",
            "Add 'ntfy' (no account needed), 'pushover' or 'telegram' to "
            "notify.channels.",
        ))
    else:
        out.append(Check("phone push", PASS, f"{', '.join(phone)}"))
    return out


def _check_exposure(config: Config) -> list[Check]:
    server = config.server
    out: list[Check] = []
    exposed = bool(server.public_base_url)

    if exposed and not server.api_token:
        out.append(Check(
            "api security", FAIL,
            "the server is published but has no API token",
            "Set server.api_token - anyone who finds the URL can otherwise read "
            "and edit your portfolio. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"`.",
        ))
    elif exposed and server.public_base_url.startswith("http://"):
        out.append(Check(
            "api security", WARN,
            f"{server.public_base_url} is plain HTTP",
            "Terminate TLS in front of it; the API token travels in a header.",
        ))
    elif exposed:
        out.append(Check("api security", PASS,
                         f"published at {server.public_base_url} behind a token"))
    else:
        out.append(Check(
            "api security", PASS, "not published; localhost only"))

    if config.notify.actionable and not exposed:
        out.append(Check(
            "tap-to-act", WARN,
            "notifications will have no action buttons",
            "Set server.public_base_url to a URL your phone can reach.",
        ))
    elif exposed:
        out.append(Check("tap-to-act", PASS, "action buttons enabled"))
    return out


def _check_schedule(config: Config) -> Check:
    if not config.schedule.enabled:
        return Check(
            "scheduler", WARN, "disabled, so nothing runs on its own",
            "Set schedule.enabled to true and run `pokeflip serve`.",
        )
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(config.timezone)
    except Exception:
        return Check(
            "scheduler", FAIL, f"unknown timezone {config.timezone!r}",
            "Use an IANA name like 'America/New_York'.",
        )
    rules = config.schedule
    return Check(
        "scheduler", PASS,
        f"refresh every {rules.refresh_interval_minutes}min, digest at "
        f"{rules.digest_hour:02d}:{rules.digest_minute:02d} {config.timezone}",
    )


# --- setup wizard -------------------------------------------------------


def wizard(config: Config, ask: Callable[[str, str], str],
           confirm: Callable[[str, bool], bool]) -> Config:
    """Walk through the settings the app cannot infer.

    ``ask`` and ``confirm`` are injected so this is testable without a
    terminal.
    """
    marketplaces = {
        "tcgplayer": (0.1025, 0.025, 0.30),
        "ebay": (0.1325, 0.0, 0.30),
        "cardmarket": (0.05, 0.0, 0.0),
    }

    venue = ask("Where do you sell? (tcgplayer/ebay/cardmarket)", "tcgplayer").lower()
    commission, payment, flat = marketplaces.get(venue, marketplaces["tcgplayer"])
    config.fees.name = venue
    config.fees.commission_pct = commission
    config.fees.payment_pct = payment
    config.fees.payment_flat = flat
    config.fees.shipping_cost = float(
        ask("What does shipping one card cost you?", f"{config.fees.shipping_cost}"))

    bankroll = ask("Working capital, for concentration warnings (0 to skip)", "0")
    config.capital.bankroll = float(bankroll or 0)

    config.timezone = ask("Your timezone (IANA name)", config.timezone)
    config.schedule.digest_hour = int(
        ask("What hour should the daily digest arrive?",
            str(config.schedule.digest_hour)))

    sets = ask("Sets to track whole, comma separated (blank to skip)",
               ",".join(config.tracked_sets))
    config.tracked_sets = [s.strip() for s in sets.split(",") if s.strip()]

    key = ask("Pokemon TCG API key, optional - raises the rate limit",
              config.provider.api_key)
    config.provider.api_key = key.strip()

    channels = ["file"]
    if confirm("Send alerts to your phone with ntfy? (no account needed)", True):
        suggested = config.notify.ntfy_topic or f"pokeflip-{secrets.token_hex(8)}"
        topic = ask("ntfy topic - keep it secret, anyone with it can read your alerts",
                    suggested)
        config.notify.ntfy_topic = topic.strip()
        channels.append("ntfy")
    config.notify.channels = channels

    if confirm("Will you reach this server from your phone (for tap-to-act)?", False):
        config.server.public_base_url = ask(
            "Public URL, e.g. https://pokeflip.example.com",
            config.server.public_base_url).strip().rstrip("/")
        # Generated rather than prompted: a token someone types is a token
        # someone can guess.
        if not config.server.api_token:
            config.server.api_token = secrets.token_urlsafe(32)

    return config

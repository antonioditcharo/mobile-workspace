"""Getting the dashboard onto your phone without typing a 43-character token.

Two problems stand between a running server and a phone:

* the server listens on ``127.0.0.1`` by default, which nothing else on the
  wifi can reach - this module works out the address that *is* reachable;
* once it is reachable, ``api_token`` protects it, and that token is long on
  purpose. Typing it on a phone keyboard is miserable and putting it in a URL
  leaves it in history.

So pairing hands over a short code instead. ``pokeflip pair`` mints one, you
open ``http://<laptop>:8787/p/<code>`` on the phone, and the server trades it
for the same session cookie the desktop uses. The code is safe to type in
public because it lives for minutes, works once, and buys nothing on its own -
it only proves you were standing at the laptop when it was printed.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
from dataclasses import dataclass
from datetime import timedelta

from .config import Config
from .db import Database, iso, utcnow

# No I, L, O, U, 0 or 1: everything left is unambiguous read aloud or squinted
# at across a room, which is how these codes actually get used.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CODE_LENGTH = 8
DEFAULT_TTL_MINUTES = 15
# 30**8 is about 6.6e11. With a quarter-hour life and a failure budget per
# client, guessing is not a threat worth lengthening the code for.
MAX_ATTEMPTS = 12
ATTEMPT_WINDOW_SECONDS = 900


@dataclass
class PairCode:
    code: str
    expires_at: str

    @property
    def pretty(self) -> str:
        """``ABCD-EFGH`` - the form printed and shown on the phone."""
        half = CODE_LENGTH // 2
        return f"{self.code[:half]}-{self.code[half:]}"

    @property
    def path(self) -> str:
        return f"/p/{self.pretty}"

    def url(self, base: str) -> str:
        return f"{base.rstrip('/')}{self.path}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.pretty, "expires_at": self.expires_at}


def normalise(raw: str) -> str:
    """Accept whatever the phone keyboard produced.

    Dashes, spaces and case are noise. Characters outside the alphabet are
    dropped rather than guessed at - a wrong length simply fails, which is the
    honest outcome when the input is ambiguous.
    """
    return "".join(ch for ch in raw.upper() if ch in CODE_ALPHABET)


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def mint(db: Database, ttl_minutes: int = DEFAULT_TTL_MINUTES) -> PairCode:
    """Create a single-use pairing code and forget every expired one."""
    purge(db)
    expires = utcnow() + timedelta(minutes=max(1, ttl_minutes))
    code = new_code()
    db.execute(
        "INSERT INTO pair_codes (code, created_at, expires_at) VALUES (?, ?, ?)",
        (code, iso(), iso(expires)),
    )
    return PairCode(code=code, expires_at=iso(expires))


def redeem(db: Database, raw: str, client: str = "") -> bool:
    """Spend a code. ``False`` for unknown, expired or already-used."""
    code = normalise(raw)
    if len(code) != CODE_LENGTH:
        return False
    # Marking used and checking validity in one statement means two phones
    # racing the same code cannot both win.
    with db.tx() as conn:
        cur = conn.execute(
            """
            UPDATE pair_codes SET used_at = ?, used_by = ?
            WHERE code = ? AND used_at IS NULL AND expires_at > ?
            """,
            (iso(), client[:64], code, iso()),
        )
        return cur.rowcount > 0


def purge(db: Database) -> int:
    """Drop codes that expired more than a day ago."""
    cutoff = iso(utcnow() - timedelta(days=1))
    with db.tx() as conn:
        return conn.execute(
            "DELETE FROM pair_codes WHERE expires_at < ?", (cutoff,)).rowcount


def active_codes(db: Database) -> list[PairCode]:
    rows = db.query(
        "SELECT code, expires_at FROM pair_codes "
        "WHERE used_at IS NULL AND expires_at > ? ORDER BY expires_at",
        (iso(),),
    )
    return [PairCode(code=r["code"], expires_at=r["expires_at"]) for r in rows]


# --- where the phone should point ---------------------------------------


def _is_usable(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_unspecified)


def _route_address() -> str:
    """The interface the OS would use to leave this machine.

    A UDP socket has no handshake, so connecting sends nothing - it just asks
    the routing table which local address a packet would go out from. That is
    the one a phone on the same wifi can reach, which hostname lookups often
    get wrong on machines with several interfaces.
    """
    for probe in ("192.168.255.255", "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe, 9))
            address = sock.getsockname()[0]
            if _is_usable(address):
                return address
        except OSError:
            continue
        finally:
            sock.close()
    return ""


def lan_addresses() -> list[str]:
    """This machine's LAN IPv4 addresses, the most likely one first."""
    found: list[str] = []

    def add(address: str) -> None:
        if _is_usable(address) and address not in found:
            found.append(address)

    add(_route_address())
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass

    # Private ranges before anything routable: a phone on the same wifi wants
    # the former, and the latter is usually a VPN or container bridge.
    found.sort(key=lambda a: not ipaddress.ip_address(a).is_private)
    return found


def listens_everywhere(config: Config) -> bool:
    return config.host in {"0.0.0.0", "::", ""}


def is_loopback_host(config: Config) -> bool:
    host = (config.host or "").strip()
    if host in {"localhost", ""}:
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def in_container() -> bool:
    """Whether the addresses we can see belong to a container, not the host.

    Inside Docker the routing table hands back the container's private address,
    which is real and completely useless to a phone - the published port lives
    on the host. Better to say so than to print a confident wrong answer.
    """
    from pathlib import Path

    if Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text()
    except OSError:
        return False


def base_urls(config: Config) -> list[str]:
    """Addresses worth handing to a phone, best first.

    A configured ``public_base_url`` wins - if you went to the trouble of
    publishing the app, that is the address that survives leaving the house.
    """
    urls: list[str] = []
    published = (config.server.public_base_url or "").strip().rstrip("/")
    if published:
        urls.append(published)
    if not listens_everywhere(config) and not is_loopback_host(config):
        urls.append(f"http://{config.host}:{config.port}")
    elif listens_everywhere(config):
        urls.extend(f"http://{a}:{config.port}" for a in lan_addresses())
    out: list[str] = []
    for url in urls:
        if url not in out:
            out.append(url)
    return out

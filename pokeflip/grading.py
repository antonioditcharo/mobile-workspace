"""Is this card worth grading?

The question is not "what does a PSA 10 sell for" - it is whether the expected
value across the grades you will *actually* get, minus fees, minus the weeks
your money is gone, beats just selling the card raw today.

Two things make this honest:

* **Grade odds are your assumption, not a fact.** The defaults are deliberately
  conservative. Once you have submission results, put your real rates in
  ``grading.grade_odds``.
* **Comps beat multipliers.** If you have recorded observed graded prices for a
  card they are used directly. Otherwise the app falls back to configured
  multiples of the raw price and says so, loudly, in the output.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import date
from typing import Any

from .analytics import load_metrics
from .config import Config
from .db import Database, iso
from .portfolio import condition_price, open_positions


@dataclass
class GradeOutcome:
    grade: str
    probability: float
    price: float
    net_proceeds: float
    source: str          # comp | multiplier

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GradingVerdict:
    card_id: str
    variant: str
    card_name: str = ""
    set_name: str = ""
    condition: str = "NM"
    raw_price: float | None = None
    raw_net: float | None = None          # what you would bank selling it raw
    grading_cost: float = 0.0
    expected_gross: float = 0.0
    expected_net: float = 0.0             # after fees and grading cost
    expected_profit: float = 0.0          # versus selling it raw today
    expected_roi: float | None = None
    breakeven_10_price: float | None = None
    turnaround_days: int = 0
    outcomes: list[GradeOutcome] = field(default_factory=list)
    verdict: str = "unknown"              # grade | marginal | sell_raw | unknown
    basis: str = "multiplier"             # comp | multiplier | mixed
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["outcomes"] = [o.to_dict() if isinstance(o, GradeOutcome) else o
                            for o in self.outcomes]
        return data


# --- comps --------------------------------------------------------------


def record_comp(db: Database, card_id: str, grade: str, price: float,
                variant: str = "normal", service: str = "PSA",
                source: str = "manual", observed_at: str | None = None) -> int:
    """Store an observed graded sale price."""
    if not db.get_card(card_id):
        raise ValueError(f"unknown card {card_id!r}")
    if price <= 0:
        raise ValueError("price must be positive")
    return db.execute(
        """
        INSERT INTO graded_comps (card_id, variant, service, grade, price, source, observed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(card_id, variant, service, grade, observed_at)
        DO UPDATE SET price = excluded.price, source = excluded.source
        """,
        (card_id, variant, service, str(grade), price, source, observed_at or iso()),
    )


def comps_for(db: Database, card_id: str, variant: str, service: str
              ) -> dict[str, float]:
    """Most recent observed price per grade."""
    rows = db.query(
        """
        SELECT grade, price FROM graded_comps
        WHERE card_id = ? AND variant = ? AND service = ?
        ORDER BY observed_at DESC
        """,
        (card_id, variant, service),
    )
    out: dict[str, float] = {}
    for row in rows:
        out.setdefault(str(row["grade"]), float(row["price"]))
    return out


def list_comps(db: Database, card_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM graded_comps"
    params: list[Any] = []
    if card_id:
        sql += " WHERE card_id = ?"
        params.append(card_id)
    return [dict(r) for r in db.query(sql + " ORDER BY observed_at DESC", params)]


# --- the calculation ----------------------------------------------------


def evaluate(db: Database, config: Config, card_id: str, variant: str = "normal",
             condition: str = "NM", today: date | None = None,
             grade_odds: dict[str, float] | None = None) -> GradingVerdict:
    """Expected value of grading one copy, against selling it raw today."""
    rules = config.grading
    card = db.get_card(card_id)
    verdict = GradingVerdict(
        card_id=card_id, variant=variant, condition=condition,
        card_name=(card["name"] if card else card_id),
        set_name=(card["set_name"] if card else ""),
        turnaround_days=rules.turnaround_days,
    )

    source = config.provider.preferred_source
    # "sv3pt5-199" almost always means the printing that trades, not `normal`.
    resolved = db.best_variant(card_id, source, variant) or variant
    if resolved != variant:
        verdict.reasons.append(f"Priced the {resolved} printing, not {variant}.")
        verdict.variant = variant = resolved

    metrics = load_metrics(db, card_id, variant, source, today=today)
    if metrics.price is None:
        verdict.verdict = "unknown"
        verdict.reasons.append("No price history for this card - sync it first.")
        return verdict

    raw_price = condition_price(config, metrics.price, condition)
    verdict.raw_price = round(raw_price, 2)
    verdict.raw_net = round(config.fees.net_proceeds(raw_price), 2)

    # Grading a played copy is throwing money away; the grade is capped by the
    # damage before the card ever reaches a grader.
    if config.conditions.multiplier(condition) < 1.0:
        verdict.reasons.append(
            f"{condition} copies do not grade well - these numbers assume a "
            "card that is genuinely near mint."
        )

    per_card_cost = rules.fee_each + rules.shipping_each
    verdict.grading_cost = round(per_card_cost, 2)

    comps = comps_for(db, card_id, variant, rules.service)
    odds = grade_odds or rules.grade_odds
    total_odds = sum(odds.values()) or 1.0

    expected_gross = 0.0
    expected_net = 0.0
    used_comp = used_multiplier = False

    for grade, weight in odds.items():
        probability = weight / total_odds
        comp = comps.get(str(grade))
        if comp:
            price = comp
            source = "comp"
            used_comp = True
        else:
            multiplier = rules.default_multipliers.get(
                str(grade), rules.default_multipliers.get("9", 1.0))
            price = metrics.price * multiplier
            source = "multiplier"
            used_multiplier = True
        net = config.fees.net_proceeds(price)
        expected_gross += probability * price
        expected_net += probability * net
        verdict.outcomes.append(GradeOutcome(
            grade=str(grade), probability=round(probability, 4),
            price=round(price, 2), net_proceeds=round(net, 2), source=source,
        ))

    verdict.basis = ("comp" if used_comp and not used_multiplier
                     else "mixed" if used_comp else "multiplier")
    verdict.expected_gross = round(expected_gross, 2)
    verdict.expected_net = round(expected_net - per_card_cost, 2)
    verdict.expected_profit = round(verdict.expected_net - (verdict.raw_net or 0.0), 2)
    if verdict.raw_net and verdict.raw_net > 0:
        verdict.expected_roi = round(verdict.expected_profit / verdict.raw_net, 4)

    # What a 10 would have to fetch to make the whole submission worthwhile,
    # holding the other grades where they are.
    top = max(odds, key=_grade_rank) if odds else None
    if top is not None:
        top_probability = odds[top] / total_odds
        if top_probability > 0:
            others = sum(
                outcome.probability * outcome.net_proceeds
                for outcome in verdict.outcomes if outcome.grade != str(top)
            )
            needed_net = (
                (verdict.raw_net or 0.0) + per_card_cost - others) / top_probability
            verdict.breakeven_10_price = round(
                config.fees.breakeven_sale_price(needed_net) if needed_net > 0 else 0.0,
                2)

    verdict.verdict = _decide(verdict, rules, metrics)
    verdict.reasons.extend(_explain(verdict, rules, metrics))
    return verdict


def _grade_rank(grade: str) -> float:
    try:
        return float(grade)
    except (TypeError, ValueError):
        return 0.0


def _decide(verdict: GradingVerdict, rules, metrics) -> str:
    if verdict.raw_price is not None and verdict.raw_price < rules.min_raw_price:
        return "sell_raw"
    if verdict.expected_profit >= rules.min_expected_profit:
        return "grade"
    if verdict.expected_profit > 0:
        return "marginal"
    return "sell_raw"


def _explain(verdict: GradingVerdict, rules, metrics) -> list[str]:
    out: list[str] = []
    if verdict.basis == "multiplier":
        out.append(
            "No graded comps on file for this card - prices are configured "
            "multiples of the raw price, which is a guess. Record real comps "
            "with `pokeflip grade comp` before trusting this."
        )
    elif verdict.basis == "mixed":
        out.append("Some grades use observed comps, others fall back to multipliers.")

    if verdict.raw_price is not None and verdict.raw_price < rules.min_raw_price:
        out.append(
            f"At ${verdict.raw_price:,.2f} raw this is under your "
            f"${rules.min_raw_price:,.2f} floor - grading fees dominate."
        )
    if verdict.verdict == "grade":
        out.append(
            f"Expected {verdict.expected_profit:+,.2f} over selling raw, with "
            f"${verdict.grading_cost:,.2f} of fees and about "
            f"{verdict.turnaround_days} days of your capital tied up."
        )
    elif verdict.verdict == "marginal":
        out.append(
            f"Only {verdict.expected_profit:+,.2f} better than selling raw - not "
            f"worth {verdict.turnaround_days} days and the risk of a low grade."
        )
    if metrics.direction == "falling":
        out.append(
            "The card is trending down; it may be worth less by the time it "
            "comes back from the grader."
        )
    out.append(
        "Grade odds are an assumption about your own cards and your own eye. "
        "Replace them with your real submission results."
    )
    return out


def scan_portfolio(db: Database, config: Config, today: date | None = None,
                   limit: int = 25) -> dict[str, Any]:
    """Which cards you already own are worth sending in."""
    rules = config.grading
    candidates: list[GradingVerdict] = []
    skipped = 0

    for position in open_positions(db):
        if position.condition.strip().upper() != "NM":
            skipped += 1
            continue
        verdict = evaluate(db, config, position.card_id, position.variant,
                           position.condition, today=today)
        if verdict.raw_price is None or verdict.raw_price < rules.min_raw_price:
            skipped += 1
            continue
        candidates.append(verdict)

    candidates.sort(key=lambda v: v.expected_profit, reverse=True)
    worth_it = [v for v in candidates if v.verdict == "grade"]
    return {
        "as_of": iso(),
        "service": rules.service,
        "candidates": [v.to_dict() for v in candidates[:limit]],
        "recommended": [v.to_dict() for v in worth_it[:limit]],
        "skipped": skipped,
        "total_expected_profit": round(sum(v.expected_profit for v in worth_it), 2),
        "submission_cost": round(
            len(worth_it) * (rules.fee_each + rules.shipping_each), 2),
        "note": (
            "Only near-mint copies are considered. Expected values use your "
            "configured grade odds, which you should replace with your own "
            "observed rates."
        ),
    }

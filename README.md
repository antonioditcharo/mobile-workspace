# pokeflip

Price, trend and signal tracking for Pokémon card flippers. It watches prices
on a schedule, scores every card you track for buys and sells, values bulk lots
before you bid on them, and hands you a short list of things to do — in the
terminal, on a dashboard, or delivered to Slack, Discord, a webhook or email.

It answers five questions:

1. **What should I buy today?** Cards trading below their own trend, listing
   floors well under market, and early upturns — filtered so the trade still
   clears marketplace fees with room to spare.
2. **What should I sell today?** Positions that hit their target, ran hot,
   started giving back a peak, went dead, or broke down far enough to cut.
3. **Is this lot worth it?** Bulk buying and bulk selling, with the haircuts
   that separate a good lot from a garage full of cardboard.
4. **What happened to the thing I listed three weeks ago?** Orders and listings
   are tracked from bid to sale, with days on market and re-pricing advice.
5. **Do these rules actually work?** The engine replays itself against history
   and grades every rule on what really happened next.

Every number is net of fees. A recommendation always shows the arithmetic
behind it, so you can argue with it instead of just trusting it.

---

## Quick start

```bash
pip install -r requirements.txt

# Seed 180 days of realistic offline data and a sample portfolio.
python -m pokeflip.cli demo

# What to do right now.
python -m pokeflip.cli scan

# The full report.
python -m pokeflip.cli digest

# Dashboard + API + scheduler on http://127.0.0.1:8787
python -m pokeflip.cli serve
```

Installing the package (`pip install -e .`) puts a `pokeflip` command on your
path, so every example below can drop the `python -m pokeflip.cli` prefix.

### Going live

`demo` runs on the built-in offline provider. To track real prices:

```bash
python -m pokeflip.cli init            # writes config.json
```

Then set `provider.name` to `pokemontcg` (the default) in `config.json` and
start tracking things:

```bash
python -m pokeflip.cli sync --set sv3pt5             # pull and price a whole set
python -m pokeflip.cli search charizard --remote     # find cards
python -m pokeflip.cli watch add sv3pt5-199 --max-buy 380 --target-sell 520
python -m pokeflip.cli hold add sv3pt5-199 --variant holofoil --quantity 2 --cost 402.50
python -m pokeflip.cli refresh                       # fetch prices now
```

### Price sources

| `provider.name` | Gives you | Needs |
|---|---|---|
| `pokemontcg` (default) | TCGplayer (USD) + Cardmarket (EUR) aggregates, and the card catalog | nothing; an API key raises the rate limit |
| `ebay` | **actual sold prices and sales velocity** | eBay app credentials; sold data needs extra approval |
| `fixture` | deterministic offline data | nothing — this is what `demo` uses |

eBay is the only source that can tell you **how fast a card sells**, which is
what separates "cheap" from "cheap because nobody wants it". It is a
marketplace rather than a card database, so keep `pokemontcg` as the catalog
source and point prices at eBay:

```jsonc
"provider": {
  "name": "ebay",
  "catalog_name": "pokemontcg",
  "ebay_client_id": "...",         // https://developer.ebay.com
  "ebay_client_secret": "...",
  "ebay_use_sold_data": true       // needs Marketplace Insights approval
}
```

Without Marketplace Insights approval the provider still works from live
listings, but it prices from the **lower quartile of asking prices** and
reports velocity as unknown rather than passing asks off as sales. Lots,
bundles, proxies and graded slabs are filtered out so a 100-card lot never sets
the price of one card; set `ebay_track_graded` to price PSA/BGS copies as their
own variants.

> **Network note.** If your environment blocks `api.pokemontcg.io`, set
> `provider.name` to `fixture` and the app runs entirely offline on generated
> data. Everything except live prices behaves identically.

---

## What it tracks

For every card printing, from your stored history:

| | |
|---|---|
| **Level** | market price, cheapest listing, mid, high, direct low |
| **Averages** | 7 / 30 / 90-day simple moving averages |
| **Change** | 1 / 7 / 30 / 90-day percentage change |
| **Position in range** | 90-day z-score, 30-day peak and trough, drawdown from peak, run-up from trough |
| **Behaviour** | momentum (7d vs 30d), fitted 30-day trend slope, daily-return volatility, trend label |
| **Opportunity** | spread between the listing floor and market price |
| **Liquidity** | sales per week and a 0–1 score, labelled `observed` when the source reports real sales and `inferred` when it is guessed from price stability and spread |

Prices are stored one snapshot per card, per printing, per marketplace, per day.
Re-running a refresh on the same day updates that day's row rather than
double-counting it in the trend math. Days with no usable price are left
missing rather than zero-filled — a gap is missing data, and pretending it was
$0 would poison every average.

---

## How buy and sell decisions are made

### Buy

A card must first pass hard gates: enough history, a fresh quote, inside your
price band, not wildly volatile, and profitable after fees on at least one of
two theses — flipping it at today's market, or mean reversion back to its own
30-day average. A card down more than 35% and still falling is rejected
outright; that is a falling knife, not a dip.

Then it scores against four reasons:

| Reason | Fires when |
|---|---|
| `buy_dip` | Trading meaningfully under its own 30-day average |
| `buy_spread` | Cheapest listing sits well below what the card actually sells for |
| `buy_undervalued` | Bottom of its own 90-day range by z-score |
| `buy_momentum` | Turning up, and not yet extended past its recent peak |

### Sell

| Reason | Fires when |
|---|---|
| `sell_target` | Net proceeds clear your target ROI over cost basis |
| `sell_strength` | Running well above its 30-day average, in profit |
| `sell_peak_fade` | Giving back a chunk of a recent peak and no longer rising |
| `sell_trend_break` | 7-day average crossed below the 30-day while in profit |
| `sell_stagnant` | Held a long time, going nowhere — recycle the capital |
| `sell_stop_loss` | Down past your stop and still falling |

### Scoring

Reasons to trade are **alternatives, not evidence that has to pile up**. A
position that hit its profit target is a sell whether or not anything else
agrees. So the strongest reason sets the score, and a second one adds to it:

```
score = 100 × ( best + 0.35 × (1 − best) × second_best )
```

where each reason's strength is how far past its threshold it is, scaled by how
much that reason alone justifies acting (a hit target counts for more than a
mild trend break). Everything is tunable in `config.json`.

Cards priced below `bulk.filler_price_ceiling` never produce individual sell
signals — the fees on a single order exceed what the card is worth. They show
up in the bulk plan instead.

---

## Does any of this work? — the scorecard

The engine grades itself. `pokeflip backtest` replays every day in the lookback
window, re-runs the rules using **only the prices that existed on that day**,
and checks what actually happened over the next 7 / 30 / 90 days:

```
KIND  REASON            HORIZON   N  GRADED  WIN RATE  MED ROI  AVG DRAWDOWN  VERDICT
buy   buy_undervalued       30d  30      30      100%   +36.4%         -1.6%  reliable
buy   buy_spread            30d   9       9      100%   +28.4%         -0.0%  reliable
sell  sell_strength         30d  11      11       91%   +19.4%         -0.7%  reliable
sell  sell_peak_fade        30d  18      18       22%    -2.9%        +14.9%  unreliable
sell  sell_stop_loss        30d  46      46       22%    -5.0%        +21.6%  unreliable
```

Read that as: cutting losers early (`sell_stop_loss`) lost money more often
than not on this data, because the cards recovered. That is the sort of thing
you cannot know by staring at thresholds, and it is exactly what should drive
you to raise `sell.stop_loss_pct` or turn the rule off.

Two honesty notes, also printed with every report:

- **No look-ahead.** Metrics on day D come from a series truncated at D. The
  forward window is read only to score the outcome.
- **Sell signals use a synthetic cost basis** — the card's own price 90 days
  before the signal — because you did not actually hold every card. They
  therefore measure *exit timing*, not a profit you made.

`insufficient_data` is a real verdict; below eight graded signals the app
declines to judge a rule at all.

```bash
pokeflip backtest --days 180 --horizon 30    # replay and grade
pokeflip backtest --stored                   # last result, no re-run
```

The scheduler re-runs it weekly.

---

## Closing the loop: orders and listings

A recommendation is not a trade. Orders are how the two get connected.

```bash
pokeflip scan                                  # "buy Giratina V at $27.72"
pokeflip order take swsh11-186                 # becomes a pending bid at that price
pokeflip order fill 2 --price 28.50            # it went through -> holding created
```

Filling a buy creates the holding with your real fill price and the signal that
prompted it attached. Filling a sell closes the lot and books the realised
profit. Partial fills leave the remainder open.

**An open sell order is a live listing**, which means days on market and price
cuts are tracked in the same place:

```bash
pokeflip listings                # every live listing, with a verdict on each
pokeflip listings --apply        # re-price the ones flagged for a cut
```

| Verdict | Means |
|---|---|
| `hold` | priced in line with the market |
| `raise` | the market moved up past your ask — you are leaving money behind |
| `cut` | stale, or priced above where it will actually sell |
| `pull` | stale *and* the card is trending down — cut hard or bulk it |

Staleness is measured from the **last price change**, not from the listing
date. Otherwise an auto-applied cut re-fires on the next cycle and ratchets
your ask down to the floor one pass at a time.

---

## Condition, grading and capital

**Condition affects value.** Quoted prices are near-mint prices;
`conditions.multipliers` discounts what you actually hold (`LP` 0.85, `MP` 0.70,
and so on). Positions are grouped by condition, because an LP copy is not the
same asset as an NM one and averaging them hides the difference.

**Grading is an expected-value question**, not a "what does a PSA 10 go for"
question:

```bash
pokeflip grade card swsh7-215        # is this one worth sending?
pokeflip grade scan                  # everything you hold, ranked
pokeflip grade comp swsh7-215 10 --price 1450   # record a real observed sale
```

It weighs each grade by your assumed odds, nets off fees, grading cost and the
weeks your capital is gone, and compares that against selling the card raw
today. **Record comps.** Without them it falls back to configured multiples of
the raw price, which are guesses — the output says so every time, and the
verdict is not worth acting on until real sales are on file.

**Capital allocation** warns when one card or one set has quietly become most
of the book:

```bash
pokeflip risk
```

```
! Charizard ex is 44% of your cost basis (limit 20%)
! 151 is 56% of your cost basis (limit 40%)
```

Set `capital.bankroll` to also track free capital and how much is tied up in
open bids.

**Selling picks lots explicitly.** Which lot goes first changes the realised
gain, so it is a choice rather than an accident of row order:

```bash
pokeflip sell sv3pt5-199 --quantity 2 --price 480 --method fifo
```

`fifo` (default), `lifo`, `highest_cost` (smallest gain now), `lowest_cost`.

**Tax-ready export** of every closed position, with holding period:

```bash
pokeflip export --year 2026 --output sales-2026.csv
```

---

## Bulk

Three haircuts separate a good lot from a bad one, and all three are applied:

- **Sell-through** — you will not sell every card. Ever.
- **Bulk discount** — moving volume means pricing under market.
- **Fees and handling** — a hundred $2 cards is a hundred orders' worth of fees.

The output is a **maximum bid**, not an appraisal: the number you can pay and
still make your target margin.

```bash
# Itemised lot, with an asking price to judge
pokeflip bulk value --file lot.csv --ask 450 --shipping 15
pokeflip bulk value --item sv3pt5-199:holofoil:1 --item swsh7-215:holofoil:2 --ask 900

# Unsorted lot described only by card count
pokeflip bulk estimate --count 5000 --set sv3pt5 --ask 120

# Which of your own cards to list individually and which to move as bulk
pokeflip bulk plan

# Where the value sits in a set — what makes a box worth opening
pokeflip bulk set sv3pt5
```

`lot.csv` is `card_id,variant,quantity` with a header row.

`bulk value` splits every line three ways: cards worth pulling and selling as
singles, mid-value cards moved in small lots, and filler worth only what a bulk
buyer pays. Cards with no price history count as filler rather than being
skipped — an unknown card is worth approximately nothing, and pretending
otherwise inflates the bid.

`bulk estimate` is a screening tool. It prices each rarity at the **25th
percentile** of your tracked cards (the holos in a box are the ones nobody
pulled for singles), reports a **data-confidence** figure, and refuses to give
a buy/pass verdict at all when your sample coverage is too thin. Itemise the
lot before committing real money.

---

## Running it on a schedule

```bash
pokeflip run      # scheduler only, foreground
pokeflip serve    # dashboard + API + scheduler in one process
```

| Job | Default | Does |
|---|---|---|
| `refresh` | every 6 hours | fetch prices, re-score, raise alerts |
| `digest-daily` | 08:00 | build and deliver the daily report |
| `digest-weekly` | Sunday 09:00 | the same with a weekly framing |
| `catalog` | every 7 days | pick up new sets and printings |
| `backtest` | Sunday 08:30 | re-grade the rules against history |

A refresh prices, in priority order: your holdings, your watchlist, cards in
lots you are evaluating, cards in `tracked_sets`, and anything already carrying
history — capped by `max_cards_per_refresh`.

Every run is recorded, so a failure is visible after the fact rather than
silent: `pokeflip runs`.

### Delivery

Set `notify.channels` to any of `console`, `file`, `webhook`, `slack`,
`discord`, `email`. **The default is `console` and `file` only** — a fresh
install never posts anywhere until you tell it where.

```jsonc
"notify": {
  "channels": ["file", "slack"],
  "slack_webhook_url": "https://hooks.slack.com/services/...",
  "report_dir": "reports",
  "realtime_alerts": true
}
```

Alerts are deduplicated for 20 hours, so a card parked below your buy price
does not page you every cycle.

---

## Command reference

```
pokeflip init                     write a starter config.json
pokeflip demo [--days N]          seed offline data with real-looking history
pokeflip search QUERY [--remote]  find cards
pokeflip sync --set ID            pull a whole set into the catalog and price it
pokeflip refresh [--deliver]      fetch prices, re-score, raise alerts
pokeflip scan                     what to buy and sell right now
pokeflip digest [--save --deliver --format markdown|html|json]
pokeflip portfolio                holdings, value and P&L
pokeflip card CARD_ID             full price detail for one card
pokeflip hold add|list|sell|remove
pokeflip watch add|list|remove
pokeflip alerts [--unread] [--ack]
pokeflip bulk value|estimate|plan|set
pokeflip order new|take|fill|reprice|cancel|list
pokeflip listings [--apply]       live listings and re-pricing advice
pokeflip sell CARD_ID --quantity N --price P [--method fifo|lifo|...]
pokeflip grade card|scan|comp     grading expected value
pokeflip backtest [--days N] [--horizon N] [--stored]
pokeflip risk                     capital allocation and concentration
pokeflip export [--year Y] [--output F]   tax-ready CSV
pokeflip runs                     recent job history
pokeflip run                      scheduler in the foreground
pokeflip serve                    dashboard, API and scheduler
pokeflip config                   show resolved configuration
```

Add `--json` to any command for machine-readable output.

---

## Dashboard and API

`pokeflip serve` puts a dashboard on `/` and the API on `/api`, with
interactive docs at `/docs`. The UI has no privileged access — anything it can
do, a script can do.

```
GET    /api/health                    counts, provider, scheduler state
GET    /api/digest                    the full digest as JSON
GET    /api/digest/html|markdown      rendered report
POST   /api/refresh                   run a refresh cycle now
GET    /api/signals                   latest stored buy/sell signals
POST   /api/signals/run               re-score without fetching
GET    /api/portfolio                 valuation and P&L
POST   /api/holdings                  add a lot
POST   /api/holdings/{id}/sell        record a sale (splits the lot)
GET    /api/watchlist                 watchlist with live metrics
POST   /api/watchlist                 watch a card with price targets
GET    /api/cards/search?q=           search (add &remote=true to hit the provider)
GET    /api/cards/{id}                metrics + full price history per printing
POST   /api/bulk/value                value an itemised lot
POST   /api/bulk/estimate             value an unsorted lot by card count
GET    /api/bulk/sell-plan            singles vs bulk split of your inventory
GET    /api/bulk/sets/{id}            value concentration in a set
GET    /api/orders                    open orders and listings
POST   /api/orders                    record a bid or listing
POST   /api/orders/from-signal        turn a recommendation into an order
POST   /api/orders/{id}/fill          it went through - update the portfolio
POST   /api/orders/{id}/reprice       change a listing's ask
GET    /api/listings                  every live listing with a verdict
POST   /api/listings/apply-suggestions  re-price the ones flagged for a cut
POST   /api/positions/sell            sell with automatic lot selection
GET    /api/portfolio/concentration   capital allocation warnings
GET    /api/portfolio/tax             closed positions with holding periods
GET    /api/grading/{id}              is this card worth grading
GET    /api/grading/scan              everything you hold, ranked
POST   /api/grading/comps             record an observed graded sale
GET    /api/backtest                  last signal scorecard
POST   /api/backtest/run              replay history and re-grade the rules
GET    /api/alerts                    recent alerts
GET    /api/runs                      job history
```

---

## Configuration

`config.json` in the working directory, or `--config PATH`, or
`POKEFLIP_CONFIG`. Any value can be overridden by environment variable, with
`__` for nesting:

```bash
POKEFLIP_BUY__MIN_ROI=0.4 POKEFLIP_FEES__COMMISSION_PCT=0.1325 pokeflip scan
```

The settings worth tuning first:

```jsonc
{
  "fees": {                        // your actual cost of completing a sale
    "commission_pct": 0.1025,      // TCGplayer seller fee; eBay is ~0.1325
    "payment_pct": 0.025,
    "payment_flat": 0.30,
    "shipping_cost": 1.10,         // stamp + sleeve + toploader
    "shipping_charged": 0.00       // 0 for free-shipping listings
  },
  "buy": {
    "dip_pct": 0.12,               // how far under its 30d average is "cheap"
    "spread_pct": 0.20,            // listing floor vs market gap worth acting on
    "min_price": 3.00,             // below this, fees eat single-card flips
    "max_price": 400.00,           // capital and risk control
    "min_net_profit": 2.00,        // dollars, after fees
    "min_roi": 0.25,
    "acquisition_overhead": 0.75,  // what it costs you to take delivery
    "max_volatility": 0.55,
    "min_score": 45
  },
  "sell": {
    "target_roi": 0.35,
    "overextended_pct": 0.18,
    "peak_fade_pct": 0.10,
    "stop_loss_pct": 0.25,
    "stagnant_days": 120
  },
  "bulk": {
    "sell_through_rate": 0.55,     // you will not sell every card
    "bulk_discount": 0.25,         // volume prices under market
    "filler_price_ceiling": 1.50,  // below this it is bulk, not a single
    "filler_value_each": 0.03,
    "target_lot_margin": 0.45,     // margin required before bidding
    "single_out_threshold": 4.00
  },
  "conditions": {
    "multipliers": { "NM": 1.0, "LP": 0.85, "MP": 0.70, "HP": 0.55, "DMG": 0.35 }
  },
  "orders": {
    "stale_listing_days": 21,      // no sale in this long needs a decision
    "stale_cut_pct": 0.08,         // how much to cut by
    "drift_tolerance": 0.07,       // re-price when the market moves this far
    "price_floor_vs_market": 0.80, // never cut below this multiple of market
    "buy_order_expiry_days": 14
  },
  "grading": {
    "service": "PSA",
    "fee_each": 25.00,
    "turnaround_days": 45,
    "grade_odds": { "10": 0.25, "9": 0.45, "8": 0.20, "7": 0.10 },
    "min_expected_profit": 20.00,  // versus just selling it raw
    "min_raw_price": 20.00
  },
  "capital": {
    "bankroll": 0,                 // set this to track free capital
    "max_position_pct": 0.20,      // warn above this share of cost basis
    "max_set_pct": 0.40,
    "lot_selection": "fifo"        // which lot to sell first
  },
  "backtest": {
    "lookback_days": 180,
    "horizons": [7, 30, 90],
    "dedupe_days": 14              // one cheap card for a month is one call
  },
  "tracked_sets": ["sv3pt5"],
  "schedule": { "refresh_interval_minutes": 360, "digest_hour": 8 }
}
```

Secrets (`api_key`, SMTP password, webhook URLs) are redacted from
`pokeflip config` and from `/api/config`.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

139 tests, standard library only, no network. The offline provider is
deterministic, so results are stable run to run.

---

## Layout

```
pokeflip/
  config.py       settings, fee model, all tunable thresholds
  db.py           SQLite access; schema.sql alongside it
  providers/      pokemontcg, ebay and fixture price sources
  ingest.py       catalog sync, price capture, demo seeding
  analytics.py    trend math — pure functions, rows in, numbers out
  signals.py      buy and sell scoring
  portfolio.py    inventory, cost basis, P&L
  bulk.py         lot valuation, bulk sell planning, set concentration
  orders.py       order and listing lifecycle, re-pricing advice
  grading.py      grading expected value and observed comps
  backtest.py     replay the rules against history and grade them
  alerts.py       watchlist and alerting
  digest.py       the report, in JSON / Markdown / HTML
  notify.py       delivery channels
  scheduler.py    the periodic jobs
  api.py          REST API and dashboard host
  cli.py          command line
  web/            dashboard (vanilla JS, no build step)
```

Everything lives in one SQLite file, so backing up is `cp data/pokeflip.db`
somewhere.

---

## Limits worth knowing

- **Prices are marketplace aggregates, not your sale.** Condition, centring,
  grading and timing all move the real number. Verify live listings before
  trading.
- **Sales-volume data depends on the source.** The Pokémon TCG API does not
  publish it, so on that source liquidity is *inferred* from price stability and
  the listing spread — a card that looks cheap may simply not sell. Point the
  provider at eBay for observed sale counts; the metric is labelled either way.
- **Backtest results are not a promise.** A window containing one big market
  move can flatter or damn any rule, and the sell scorecard grades exit timing
  against a synthetic cost basis rather than trades you actually made.
- **Grade odds are assumptions.** Until you record real comps and your own
  submission results, the grading verdict is arithmetic on guesses.
- **Signals are backward-looking.** Reprint announcements, tournament results
  and set rotations move prices before any trend does. The app flags a sharp
  move; it cannot tell you why.
- **Bulk estimates by card count assume a rarity mix.** They are for screening
  a listing, not for placing a bid.

-- pokeflip storage schema. Applied idempotently on every connect.

CREATE TABLE IF NOT EXISTS sets (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    series        TEXT,
    printed_total INTEGER,
    total         INTEGER,
    release_date  TEXT,
    symbol_url    TEXT,
    logo_url      TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    set_id         TEXT REFERENCES sets(id),
    set_name       TEXT,
    number         TEXT,
    rarity         TEXT,
    supertype      TEXT,
    subtypes       TEXT,
    artist         TEXT,
    image_small    TEXT,
    image_large    TEXT,
    tcgplayer_url  TEXT,
    cardmarket_url TEXT,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name);
CREATE INDEX IF NOT EXISTS idx_cards_set ON cards(set_id);

-- One row per (card, source, variant) per capture. Prices are stored exactly as
-- reported; all derived numbers are computed at read time so the raw record
-- stays trustworthy.
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     TEXT NOT NULL REFERENCES cards(id),
    source      TEXT NOT NULL,
    variant     TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    captured_on TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'USD',
    market      REAL,
    low         REAL,
    mid         REAL,
    high        REAL,
    direct_low  REAL,
    UNIQUE(card_id, source, variant, captured_on)
);
CREATE INDEX IF NOT EXISTS idx_price_card_time
    ON price_history(card_id, source, variant, captured_on);
CREATE INDEX IF NOT EXISTS idx_price_time ON price_history(captured_on);

CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     TEXT NOT NULL REFERENCES cards(id),
    variant     TEXT NOT NULL DEFAULT 'any',
    max_buy     REAL,
    target_sell REAL,
    note        TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    UNIQUE(card_id, variant)
);

-- A holding is one lot bought at one price. Selling part of a lot splits it so
-- cost basis stays exact.
CREATE TABLE IF NOT EXISTS holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id         TEXT NOT NULL REFERENCES cards(id),
    variant         TEXT NOT NULL DEFAULT 'normal',
    condition       TEXT NOT NULL DEFAULT 'NM',
    quantity        INTEGER NOT NULL DEFAULT 1,
    cost_each       REAL NOT NULL DEFAULT 0,
    acquired_at     TEXT NOT NULL,
    acquired_from   TEXT,
    notes           TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    sold_at         TEXT,
    sold_price_each REAL,
    sold_net_each   REAL,
    lot_id          INTEGER REFERENCES bulk_lots(id)
);
CREATE INDEX IF NOT EXISTS idx_holdings_card ON holdings(card_id, status);

CREATE TABLE IF NOT EXISTS bulk_lots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'buy',
    description  TEXT,
    ask_price    REAL,
    shipping     REAL NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'evaluating',
    created_at   TEXT NOT NULL,
    evaluated_at TEXT,
    valuation    TEXT
);

CREATE TABLE IF NOT EXISTS bulk_lot_items (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id   INTEGER NOT NULL REFERENCES bulk_lots(id) ON DELETE CASCADE,
    card_id  TEXT NOT NULL REFERENCES cards(id),
    variant  TEXT NOT NULL DEFAULT 'normal',
    quantity INTEGER NOT NULL DEFAULT 1,
    UNIQUE(lot_id, card_id, variant)
);

CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES runs(id),
    kind       TEXT NOT NULL,
    card_id    TEXT NOT NULL REFERENCES cards(id),
    variant    TEXT NOT NULL,
    score      REAL NOT NULL,
    action     TEXT NOT NULL,
    price      REAL,
    reasons    TEXT,
    metrics    TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_run ON signals(run_id, kind);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'info',
    card_id         TEXT REFERENCES cards(id),
    variant         TEXT,
    title           TEXT NOT NULL,
    body            TEXT,
    payload         TEXT,
    dedupe_key      TEXT,
    created_at      TEXT NOT NULL,
    acknowledged_at TEXT,
    delivered_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_dedupe ON alerts(dedupe_key, created_at);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats       TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_kind ON runs(kind, started_at);

CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    path       TEXT,
    summary    TEXT,
    payload    TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- An order is one intent to trade. A buy order becomes a holding when it
-- fills; an open sell order is a live listing, which is why days on market and
-- price-cut history live here rather than in a separate table.
CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,                     -- buy | sell
    status         TEXT NOT NULL DEFAULT 'open',      -- open | filled | cancelled | expired
    card_id        TEXT NOT NULL REFERENCES cards(id),
    variant        TEXT NOT NULL DEFAULT 'normal',
    condition      TEXT NOT NULL DEFAULT 'NM',
    quantity       INTEGER NOT NULL DEFAULT 1,
    limit_price    REAL,                              -- max bid, or the ask on a listing
    original_price REAL,                              -- the first ask, before any cuts
    marketplace    TEXT NOT NULL DEFAULT '',
    holding_id     INTEGER REFERENCES holdings(id),   -- source lot for a listing
    signal_action  TEXT,                              -- signal that prompted it
    signal_score   REAL,
    reference_price REAL,                             -- market price when created
    filled_price   REAL,
    filled_quantity INTEGER,
    fees           REAL,
    price_cuts     TEXT,                              -- JSON history of ask changes
    notes          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    closed_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, kind);
CREATE INDEX IF NOT EXISTS idx_orders_card ON orders(card_id, status);

-- Observed prices for graded copies. Without these, grading ROI falls back to
-- configured multipliers, which are assumptions rather than comps.
CREATE TABLE IF NOT EXISTS graded_comps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    TEXT NOT NULL REFERENCES cards(id),
    variant    TEXT NOT NULL DEFAULT 'normal',
    service    TEXT NOT NULL DEFAULT 'PSA',
    grade      TEXT NOT NULL,
    price      REAL NOT NULL,
    source     TEXT,
    observed_at TEXT NOT NULL,
    UNIQUE(card_id, variant, service, grade, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_graded_card ON graded_comps(card_id, variant, service, grade);

-- One row per signal replayed against history, with what actually happened.
CREATE TABLE IF NOT EXISTS backtest_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER REFERENCES runs(id),
    kind         TEXT NOT NULL,
    action       TEXT NOT NULL,
    card_id      TEXT NOT NULL,
    variant      TEXT NOT NULL,
    signal_date  TEXT NOT NULL,
    score        REAL,
    entry_price  REAL,
    signal_price REAL,
    horizon_days INTEGER NOT NULL,
    exit_price   REAL,
    forward_return REAL,
    net_profit   REAL,
    roi          REAL,
    max_favorable REAL,
    max_adverse  REAL,
    outcome      TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backtest_run ON backtest_results(run_id, action, horizon_days);

-- One-tap buttons on a push notification. Each token authorises exactly one
-- state change, once, before it expires. The token IS the authorisation, so it
-- must be unguessable and must never be reusable.
CREATE TABLE IF NOT EXISTS action_tokens (
    token      TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    label      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    result     TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_expires ON action_tokens(expires_at);

-- Short-lived codes that trade for a dashboard session on a phone. They are
-- deliberately typeable, which is only safe because they expire in minutes and
-- can be spent once.
CREATE TABLE IF NOT EXISTS pair_codes (
    code       TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    used_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pair_expires ON pair_codes(expires_at);

-- Alerts you have told the app to stop raising. Acknowledging only clears the
-- badge; the underlying condition would re-raise it on the next cycle.
CREATE TABLE IF NOT EXISTS alert_snoozes (
    dedupe_key TEXT PRIMARY KEY,
    until      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

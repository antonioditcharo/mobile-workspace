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

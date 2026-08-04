/* pokeflip dashboard - vanilla JS, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// --- formatting ---------------------------------------------------------

const money = (v, dash = "-") =>
  v === null || v === undefined || Number.isNaN(v)
    ? dash
    : `$${Number(v).toLocaleString("en-US", { minimumFractionDigits: 2,
                                              maximumFractionDigits: 2 })}`;

const pct = (v, places = 1) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "-"
    : `${(Number(v) * 100).toFixed(places)}%`.replace(/^(?!-)/, "+");

const signClass = (v) => (Number(v) >= 0 ? "up" : "down");
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// --- network ------------------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_) { /* body was not JSON */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

let toastTimer;
function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast${bad ? " bad" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
}

// --- table builder ------------------------------------------------------

function table(columns, rows, emptyText = "Nothing here yet.") {
  if (!rows.length) return `<div class="empty">${esc(emptyText)}</div>`;
  const cls = (c) => [c.num ? "num" : "", c.cls || ""].filter(Boolean).join(" ");
  const head = columns
    .map((c) => `<th class="${cls(c)}">${esc(c.label)}</th>`)
    .join("");
  const body = rows
    .map((row) => {
      const cells = columns
        .map((c) => `<td class="${cls(c)}">${c.render(row)}</td>`)
        .join("");
      return `<tr>${cells}</tr>`;
    })
    .join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

const cardCell = (r) =>
  `<div class="name">${esc(r.card_name || r.name || r.card_id)}</div>
   <div class="meta">${esc(r.set_name || "")} ${esc(r.number || "")}</div>`;

// --- chart --------------------------------------------------------------

/**
 * Price history with 7d and 30d moving averages.
 * One y-axis (all three series are the same measure in the same units),
 * crosshair + tooltip on hover, legend always present.
 */
function priceChart(history, currencySymbol = "$") {
  const points = history.filter((p) => p.market != null);
  if (points.length < 2) {
    return `<div class="empty">Not enough history to plot yet.</div>`;
  }

  const rolling = (values, window) =>
    values.map((_, i) => {
      const slice = values.slice(Math.max(0, i - window + 1), i + 1);
      return slice.reduce((a, b) => a + b, 0) / slice.length;
    });

  const prices = points.map((p) => p.market);
  const sma7 = rolling(prices, 7);
  const sma30 = rolling(prices, 30);

  const W = 720, H = 210, ML = 46, MR = 12, MT = 10, MB = 22;
  const plotW = W - ML - MR, plotH = H - MT - MB;

  const all = [...prices, ...sma7, ...sma30];
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo) * 0.08 || hi * 0.05 || 1;
  lo = Math.max(0, lo - pad);
  hi = hi + pad;

  const x = (i) => ML + (i / (points.length - 1)) * plotW;
  const y = (v) => MT + plotH - ((v - lo) / (hi - lo || 1)) * plotH;
  const path = (values) =>
    values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");

  // Four gridlines is enough to read a level without competing with the data.
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => lo + (hi - lo) * f);
  const gridlines = ticks
    .map(
      (t) => `<line class="grid-line" x1="${ML}" x2="${W - MR}"
                y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>
              <text class="axis-text" x="${ML - 6}" y="${(y(t) + 3).toFixed(1)}"
                text-anchor="end">${currencySymbol}${t < 10 ? t.toFixed(2) : Math.round(t)}</text>`
    )
    .join("");

  const labelEvery = Math.max(1, Math.floor(points.length / 6));
  const dateLabels = points
    .map((p, i) =>
      i % labelEvery === 0 || i === points.length - 1
        ? `<text class="axis-text" x="${x(i).toFixed(1)}" y="${H - 6}"
             text-anchor="middle">${p.on.slice(5)}</text>`
        : ""
    )
    .join("");

  const id = `chart-${Math.random().toString(36).slice(2, 9)}`;
  const series = JSON.stringify(
    points.map((p, i) => ({
      on: p.on,
      market: p.market,
      sma7: sma7[i],
      sma30: sma30[i],
      x: +x(i).toFixed(2),
      ym: +y(p.market).toFixed(2),
      y7: +y(sma7[i]).toFixed(2),
      y30: +y(sma30[i]).toFixed(2),
    }))
  );

  const last = points.length - 1;
  return `
  <div class="chart-wrap" style="position:relative">
    <div class="legend">
      <span><i class="k1"></i>Market <b>${currencySymbol}${prices[last].toFixed(2)}</b></span>
      <span><i class="k2"></i>7-day avg <b>${currencySymbol}${sma7[last].toFixed(2)}</b></span>
      <span><i class="k3"></i>30-day avg <b>${currencySymbol}${sma30[last].toFixed(2)}</b></span>
    </div>
    <svg class="chart" id="${id}" viewBox="0 0 ${W} ${H}" role="img"
         aria-label="Price history with 7 and 30 day moving averages"
         data-series='${esc(series)}' data-sym="${currencySymbol}">
      ${gridlines}
      ${dateLabels}
      <path class="series s3" d="${path(sma30)}"/>
      <path class="series s2" d="${path(sma7)}"/>
      <path class="series s1" d="${path(prices)}"/>
      <g class="focus" style="display:none">
        <line class="crosshair" y1="${MT}" y2="${MT + plotH}"/>
        <circle class="focus-dot d1" r="4" fill="var(--series-1)"/>
        <circle class="focus-dot d2" r="3.5" fill="var(--series-2)"/>
        <circle class="focus-dot d3" r="3.5" fill="var(--series-3)"/>
      </g>
      <rect class="hit" x="${ML}" y="${MT}" width="${plotW}" height="${plotH}"/>
    </svg>
    <div class="tooltip hidden"></div>
  </div>`;
}

/** Wire hover behaviour for every chart currently in the DOM. */
function bindCharts(root = document) {
  $$(".chart", root).forEach((svg) => {
    if (svg.dataset.bound) return;
    svg.dataset.bound = "1";

    const series = JSON.parse(svg.dataset.series);
    const symbol = svg.dataset.sym || "$";
    const wrap = svg.closest(".chart-wrap");
    const tip = $(".tooltip", wrap);
    const focus = $(".focus", svg);
    const hit = $(".hit", svg);

    const move = (event) => {
      const box = svg.getBoundingClientRect();
      const clientX = event.touches ? event.touches[0].clientX : event.clientX;
      const viewX = ((clientX - box.left) / box.width) * svg.viewBox.baseVal.width;
      let nearest = series[0];
      for (const point of series) {
        if (Math.abs(point.x - viewX) < Math.abs(nearest.x - viewX)) nearest = point;
      }

      focus.style.display = "";
      $(".crosshair", focus).setAttribute("x1", nearest.x);
      $(".crosshair", focus).setAttribute("x2", nearest.x);
      $(".d1", focus).setAttribute("cx", nearest.x);
      $(".d1", focus).setAttribute("cy", nearest.ym);
      $(".d2", focus).setAttribute("cx", nearest.x);
      $(".d2", focus).setAttribute("cy", nearest.y7);
      $(".d3", focus).setAttribute("cx", nearest.x);
      $(".d3", focus).setAttribute("cy", nearest.y30);

      tip.innerHTML = `
        <div class="t-date">${esc(nearest.on)}</div>
        <div class="t-row"><span>Market</span><b>${symbol}${nearest.market.toFixed(2)}</b></div>
        <div class="t-row"><span>7-day</span><b>${symbol}${nearest.sma7.toFixed(2)}</b></div>
        <div class="t-row"><span>30-day</span><b>${symbol}${nearest.sma30.toFixed(2)}</b></div>`;
      tip.classList.remove("hidden");

      const scale = box.width / svg.viewBox.baseVal.width;
      const left = nearest.x * scale;
      const flip = left > box.width - 140;
      tip.style.left = `${flip ? left - tip.offsetWidth - 12 : left + 12}px`;
      tip.style.top = `${Math.max(0, nearest.ym * scale - 10)}px`;
    };

    const leave = () => {
      focus.style.display = "none";
      tip.classList.add("hidden");
    };

    hit.addEventListener("mousemove", move);
    hit.addEventListener("touchmove", move, { passive: true });
    hit.addEventListener("mouseleave", leave);
    hit.addEventListener("touchend", leave);
  });
}

// --- views --------------------------------------------------------------

const state = { digest: null, health: null };

async function loadHealth() {
  try {
    const health = await api("/api/health");
    state.health = health;
    const counts = health.counts || {};
    $("#status").innerHTML = `
      <span class="dot ${health.scheduler_running ? "" : "off"}"></span>
      ${esc(health.provider)} &middot; ${counts.cards || 0} cards &middot;
      ${counts.holdings || 0} lots &middot;
      scheduler ${health.scheduler_running ? "on" : "off"}`;
  } catch (err) {
    $("#status").textContent = `offline: ${err.message}`;
  }
}

async function viewToday(force = false) {
  if (!state.digest || force) {
    $("#action-list").innerHTML = `<li><span class="spin"></span> Scoring…</li>`;
    state.digest = (await api("/api/digest")).digest;
  }
  const d = state.digest;
  const p = d.portfolio || {};

  $("#today-kpis").innerHTML = `
    <div class="kpi"><div class="label">Net if sold now</div>
      <div class="value">${money(p.net_liquidation)}</div>
      <div class="sub">${p.cards || 0} cards, ${money(p.cost_basis)} in</div></div>
    <div class="kpi"><div class="label">Unrealised</div>
      <div class="value ${signClass(p.unrealized_pnl)}">${money(p.unrealized_pnl)}</div>
      <div class="sub">${pct(p.unrealized_roi)} on cost</div></div>
    <div class="kpi"><div class="label">Sell candidates</div>
      <div class="value">${d.sells.length}</div>
      <div class="sub">${money(d.sells.reduce((a, s) => a + s.total_net_profit, 0))} on the table</div></div>
    <div class="kpi"><div class="label">Buy candidates</div>
      <div class="value">${d.buys.length}</div>
      <div class="sub">${money(d.buys.reduce((a, s) => a + s.net_profit, 0))} combined edge</div></div>
    <div class="kpi"><div class="label">Realised</div>
      <div class="value ${signClass(p.realized?.realized_pnl)}">${money(p.realized?.realized_pnl)}</div>
      <div class="sub">${p.realized?.sales || 0} sales</div></div>`;

  $("#action-list").innerHTML = d.actions.length
    ? d.actions
        .map(
          (a) => `<li>
            <div class="head"><span class="pill ${esc(a.type)}">${esc(a.type)}</span>
              <span class="name">${esc(a.card)}</span></div>
            <div class="detail">${esc(a.detail)}</div>
            ${a.why ? `<div class="why">${esc(a.why)}</div>` : ""}
          </li>`
        )
        .join("")
    : `<li class="empty">Nothing clears your thresholds today. Sitting out is a position.</li>`;

  $("#sell-table").innerHTML = table(
    [
      { label: "Card", render: cardCell },
      { label: "Qty", num: true, render: (r) => r.quantity },
      { label: "Market", num: true, render: (r) => money(r.price) },
      { label: "Net ea", num: true, render: (r) => money(r.net_proceeds) },
      { label: "Cost ea", num: true, render: (r) => money(r.total_cost) },
      { label: "ROI", num: true,
        render: (r) => `<span class="${signClass(r.roi)}">${pct(r.roi, 0)}</span>` },
      { label: "Total", num: true,
        render: (r) => `<span class="${signClass(r.total_net_profit)}">${money(r.total_net_profit)}</span>` },
      { label: "Score", num: true, render: (r) => `<span class="score">${r.score.toFixed(0)}</span>` },
      { label: "Why", cls: "reason",
        render: (r) => `<span class="meta">${esc(r.reasons[0] || "")}</span>` },
    ],
    d.sells,
    "No positions are ready to sell."
  );

  $("#buy-table").innerHTML = table(
    [
      { label: "Card", render: cardCell },
      { label: "Buy at", num: true, render: (r) => money(r.entry_price) },
      { label: "Market", num: true, render: (r) => money(r.price) },
      { label: "Net", num: true, render: (r) => `<span class="up">${money(r.net_profit)}</span>` },
      { label: "ROI", num: true, render: (r) => pct(r.roi, 0) },
      { label: "Score", num: true, render: (r) => `<span class="score">${r.score.toFixed(0)}</span>` },
      { label: "Why", cls: "reason",
        render: (r) => `<span class="meta">${esc(r.reasons[0] || "")}</span>` },
    ],
    d.buys,
    "Nothing is cheap enough to clear your fee and ROI floors."
  );

  const movers = [
    ...(d.movers?.gainers || []).map((m) => ({ ...m, side: "up" })),
    ...(d.movers?.losers || []).map((m) => ({ ...m, side: "down" })),
  ];
  $("#movers-table").innerHTML = table(
    [
      { label: "Card", render: cardCell },
      { label: "Price", num: true, render: (r) => money(r.price) },
      { label: "7d", num: true,
        render: (r) => `<span class="${signClass(r.change_7d)}">${pct(r.change_7d)}</span>` },
      { label: "30d", num: true,
        render: (r) => `<span class="${signClass(r.change_30d)}">${pct(r.change_30d)}</span>` },
      { label: "Trend", render: (r) => `<span class="pill ${esc(r.direction)}">${esc(r.direction)}</span>` },
    ],
    movers,
    "No movers - not enough history yet."
  );

  const runs = await api("/api/runs?limit=8");
  $("#runs-table").innerHTML = table(
    [
      { label: "Job", render: (r) => esc(r.kind) },
      { label: "Status", render: (r) => esc(r.status) },
      { label: "Started", render: (r) => esc((r.started_at || "").slice(0, 16).replace("T", " ")) },
      { label: "Detail", render: (r) =>
          `<span class="meta">${esc(r.error || JSON.stringify(r.stats || {}).slice(0, 70))}</span>` },
    ],
    runs,
    "No jobs have run yet."
  );
}

async function viewPortfolio() {
  const p = await api("/api/portfolio");
  $("#pf-kpis").innerHTML = `
    <div class="kpi"><div class="label">Cost basis</div>
      <div class="value">${money(p.cost_basis)}</div>
      <div class="sub">${p.positions} positions, ${p.cards} cards</div></div>
    <div class="kpi"><div class="label">Market value</div>
      <div class="value">${money(p.market_value)}</div></div>
    <div class="kpi"><div class="label">Net if sold now</div>
      <div class="value">${money(p.net_liquidation)}</div>
      <div class="sub">fees take ${money(p.fee_drag)}</div></div>
    <div class="kpi"><div class="label">Unrealised</div>
      <div class="value ${signClass(p.unrealized_pnl)}">${money(p.unrealized_pnl)}</div>
      <div class="sub">${pct(p.unrealized_roi)}</div></div>
    <div class="kpi"><div class="label">Realised</div>
      <div class="value ${signClass(p.realized.realized_pnl)}">${money(p.realized.realized_pnl)}</div>
      <div class="sub">${p.realized.sales} sales, win rate ${pct(p.realized.win_rate, 0)}</div></div>`;

  $("#pf-positions").innerHTML = table(
    [
      { label: "Card", render: cardCell },
      { label: "Qty", num: true, render: (r) => r.quantity },
      { label: "Cost ea", num: true, render: (r) => money(r.cost_each) },
      { label: "Market", num: true, render: (r) => money(r.price) },
      { label: "Net", num: true, render: (r) => money(r.net_value) },
      { label: "P&L", num: true,
        render: (r) => `<span class="${signClass(r.unrealized)}">${money(r.unrealized)}</span>` },
      { label: "ROI", num: true, render: (r) => pct(r.roi, 0) },
      { label: "30d", num: true, render: (r) => pct(r.change_30d) },
      { label: "Trend", render: (r) => `<span class="pill ${esc(r.direction)}">${esc(r.direction)}</span>` },
      { label: "Held", num: true, render: (r) => (r.hold_days ?? "-") + "d" },
      { label: "", render: (r) =>
          `<button class="btn tiny" data-open-card="${esc(r.card_id)}">View</button>` },
    ],
    p.positions_detail,
    "No holdings yet - add one above."
  );

  const lots = await api("/api/holdings");
  $("#pf-lots").innerHTML = table(
    [
      { label: "#", num: true, render: (r) => r.id },
      { label: "Card", render: (r) => cardCell({ card_name: r.card_name, set_name: r.set_name }) },
      { label: "Variant", render: (r) => esc(r.variant) },
      { label: "Qty", num: true, render: (r) => r.quantity },
      { label: "Cost ea", num: true, render: (r) => money(r.cost_each) },
      { label: "Acquired", render: (r) => esc((r.acquired_at || "").slice(0, 10)) },
      { label: "", render: (r) =>
          `<button class="btn tiny" data-sell="${r.id}" data-qty="${r.quantity}">Sell</button>
           <button class="btn tiny" data-drop-lot="${r.id}">Delete</button>` },
    ],
    lots,
    "No open lots."
  );

  $("#pf-sets").innerHTML = table(
    [
      { label: "Set", render: (r) => esc(r.set_name) },
      { label: "Cards", num: true, render: (r) => r.cards },
      { label: "Cost", num: true, render: (r) => money(r.cost) },
      { label: "Net", num: true, render: (r) => money(r.net_value) },
      { label: "P&L", num: true,
        render: (r) => `<span class="${signClass(r.pnl)}">${money(r.pnl)}</span>` },
    ],
    p.by_set,
    "Nothing to break down yet."
  );
}

async function viewWatchlist() {
  const rows = await api("/api/watchlist");
  $("#watch-table").innerHTML = table(
    [
      { label: "Card", render: (r) => cardCell({ card_name: r.name, set_name: r.set_name,
                                                 number: r.number, card_id: r.card_id }) },
      { label: "Market", num: true, render: (r) => money(r.metrics.price) },
      { label: "Low", num: true, render: (r) => money(r.metrics.low) },
      { label: "Max buy", num: true, render: (r) => money(r.max_buy) },
      { label: "Target", num: true, render: (r) => money(r.target_sell) },
      { label: "7d", num: true,
        render: (r) => `<span class="${signClass(r.metrics.change_7d)}">${pct(r.metrics.change_7d)}</span>` },
      { label: "Trend", render: (r) => `<span class="pill ${esc(r.metrics.direction)}">${esc(r.metrics.direction)}</span>` },
      { label: "Status", render: (r) =>
          r.buy_ready ? `<span class="pill buy">buy now</span>`
          : r.sell_ready ? `<span class="pill sell">sell now</span>` : "" },
      { label: "", render: (r) =>
          `<button class="btn tiny" data-open-card="${esc(r.card_id)}">View</button>
           <button class="btn tiny" data-unwatch="${esc(r.card_id)}"
             data-variant="${esc(r.variant)}">Remove</button>` },
    ],
    rows,
    "Nothing on the watchlist. Add a card to get price-target alerts."
  );
}

async function viewCards() { /* search-driven; nothing to preload */ }

async function openCard(cardId) {
  switchView("cards");
  const panel = $("#card-detail-panel");
  panel.classList.remove("hidden");
  $("#card-detail").innerHTML = `<div class="empty"><span class="spin"></span> Loading…</div>`;

  const data = await api(`/api/cards/${encodeURIComponent(cardId)}`);
  const card = data.card;
  $("#card-detail-title").textContent = `${card.name} - ${card.set_name} ${card.number}`;
  $("#card-detail-actions").innerHTML = `
    <button class="btn ghost" data-watch-card="${esc(card.id)}">Watch</button>
    ${card.tcgplayer_url ? `<a class="btn ghost" target="_blank" rel="noopener"
        href="${esc(card.tcgplayer_url)}">TCGplayer</a>` : ""}
    ${card.cardmarket_url ? `<a class="btn ghost" target="_blank" rel="noopener"
        href="${esc(card.cardmarket_url)}">Cardmarket</a>` : ""}`;

  const variants = data.variants
    .map((m) => {
      const symbol = m.currency === "EUR" ? "€" : "$";
      return `
      <div class="variant-block">
        <div class="variant-name">${esc(m.variant)}
          <span class="pill ${esc(m.direction)}">${esc(m.direction)}</span>
          <span class="meta">${m.points} price points${
            m.stale_days > 2 ? ` &middot; ${m.stale_days}d stale` : ""}</span></div>
        ${priceChart(m.history || [], symbol)}
        <div class="stat-grid">
          <div><div class="k">Market</div><div class="v">${money(m.price)}</div></div>
          <div><div class="k">Listing floor</div><div class="v">${money(m.low)}</div></div>
          <div><div class="k">30d avg</div><div class="v">${money(m.sma30)}</div></div>
          <div><div class="k">7d change</div>
            <div class="v ${signClass(m.change_7d)}">${pct(m.change_7d)}</div></div>
          <div><div class="k">30d change</div>
            <div class="v ${signClass(m.change_30d)}">${pct(m.change_30d)}</div></div>
          <div><div class="k">90d change</div>
            <div class="v ${signClass(m.change_90d)}">${pct(m.change_90d)}</div></div>
          <div><div class="k">30d peak</div><div class="v">${money(m.peak_30)}</div></div>
          <div><div class="k">Off peak</div><div class="v">${pct(m.drawdown_30)}</div></div>
          <div><div class="k">Spread</div><div class="v">${pct(m.spread_pct)}</div></div>
          <div><div class="k">Z-score 90d</div>
            <div class="v">${m.zscore_90 == null ? "-" : m.zscore_90.toFixed(2)}</div></div>
          <div><div class="k">Volatility</div><div class="v">${pct(m.volatility)}</div></div>
        </div>
      </div>`;
    })
    .join("");

  $("#card-detail").innerHTML = `
    <div class="detail-grid">
      <div>
        ${card.image_small ? `<img src="${esc(card.image_small)}" alt="">` : ""}
        <div class="meta" style="margin-top:8px">
          ${esc(card.rarity || "")}<br>${esc(card.id)}
        </div>
      </div>
      <div>${variants || `<div class="empty">No price history yet. Run a refresh.</div>`}</div>
    </div>`;
  bindCharts(panel);
}

async function viewBulk() { /* form-driven */ }

async function viewAlerts() {
  const rows = await api("/api/alerts?limit=60");
  $("#alerts-table").innerHTML = table(
    [
      { label: "When", render: (r) => esc((r.created_at || "").slice(0, 16).replace("T", " ")) },
      { label: "Severity", render: (r) => `<span class="pill ${
          r.severity === "urgent" ? "sell" : r.severity === "warn" ? "alert" : "bulk"
        }">${esc(r.severity)}</span>` },
      { label: "Kind", render: (r) => `<span class="meta">${esc(r.kind)}</span>` },
      { label: "What", render: (r) =>
          `<div class="name">${esc(r.title)}</div>
           <div class="meta">${esc(r.body || "")}</div>` },
      { label: "", render: (r) => r.card_id
          ? `<button class="btn tiny" data-open-card="${esc(r.card_id)}">View</button>` : "" },
    ],
    rows,
    "No alerts. Add watchlist targets to get them."
  );
}

// --- bulk renderers -----------------------------------------------------

function renderLotValuation(v) {
  const verdict = v.ask_price != null
    ? `<div class="kpi"><div class="label">At ${money(v.ask_price)} ask</div>
         <div class="value verdict ${esc(v.verdict)}">${esc(v.verdict.toUpperCase())}</div>
         <div class="sub">${pct(v.margin_at_ask, 0)} margin, ${money(v.profit_at_ask)}</div></div>`
    : "";
  return `
    <div class="kpis" style="margin-top:12px">
      <div class="kpi"><div class="label">Sticker value</div>
        <div class="value">${money(v.market_value)}</div>
        <div class="sub">${v.cards} cards</div></div>
      <div class="kpi"><div class="label">Realistic net</div>
        <div class="value">${money(v.realizable_value)}</div>
        <div class="sub">after sell-through, discount, fees</div></div>
      <div class="kpi"><div class="label">Maximum bid</div>
        <div class="value up">${money(v.max_bid)}</div>
        <div class="sub">to hit your target margin</div></div>
      ${verdict}
    </div>
    ${v.top_cards?.length ? `<div style="margin-top:12px">${table(
      [
        { label: "Card", render: (r) => cardCell(r) },
        { label: "Qty", num: true, render: (r) => r.quantity },
        { label: "Price", num: true, render: (r) => money(r.price) },
        { label: "Value", num: true, render: (r) => money(r.market_value) },
        { label: "Plan", render: (r) => `<span class="pill">${esc(r.treatment)}</span>` },
      ],
      v.top_cards
    )}</div>` : ""}
    ${v.notes?.length
      ? `<ul class="notes">${v.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`
      : ""}`;
}

function renderEstimate(e) {
  const verdictClass = { buy: "buy", thin: "thin", pass: "pass" }[e.verdict] || "thin";
  const verdict = e.verdict
    ? `<div class="kpi"><div class="label">At ${money(e.ask_price)} ask</div>
         <div class="value verdict ${verdictClass}">${
           esc(e.verdict.replace(/_/g, " ").toUpperCase())}</div>
         <div class="sub">${pct(e.margin_at_ask, 0)} margin, ${money(e.profit_at_ask)}</div></div>`
    : "";
  return `
    <div class="kpis" style="margin-top:12px">
      <div class="kpi"><div class="label">Sticker value</div>
        <div class="value">${money(e.market_value)}</div></div>
      <div class="kpi"><div class="label">Realistic net</div>
        <div class="value">${money(e.realizable_value)}</div>
        <div class="sub">${money(e.value_per_card)} per card</div></div>
      <div class="kpi"><div class="label">Maximum bid</div>
        <div class="value up">${money(e.max_bid)}</div>
        <div class="sub">${money(e.max_bid_per_card)} per card</div></div>
      ${verdict}
      <div class="kpi"><div class="label">Data confidence</div>
        <div class="value">${(e.confidence * 100).toFixed(0)}%</div>
        <div class="sub">tracked price coverage</div></div>
    </div>
    <div style="margin-top:12px">${table(
      [
        { label: "Rarity", render: (r) => esc(r.rarity) },
        { label: "Cards", num: true, render: (r) => r.cards.toFixed(0) },
        { label: "Share", num: true, render: (r) => `${(r.assumed_share * 100).toFixed(0)}%` },
        { label: "Ref price", num: true, render: (r) => money(r.reference_price) },
        { label: "Net", num: true, render: (r) => money(r.realizable) },
        { label: "Samples", num: true, render: (r) => r.samples },
      ],
      e.breakdown
    )}</div>
    <ul class="notes">${(e.caveats || [e.caveat]).map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`;
}

// --- routing ------------------------------------------------------------

const VIEWS = {
  today: viewToday,
  portfolio: viewPortfolio,
  watchlist: viewWatchlist,
  cards: viewCards,
  bulk: viewBulk,
  alerts: viewAlerts,
};

async function switchView(name) {
  $$(".view").forEach((v) => v.classList.add("hidden"));
  $(`#view-${name}`).classList.remove("hidden");
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  location.hash = name;
  try {
    await VIEWS[name]();
  } catch (err) {
    toast(err.message, true);
  }
}

// --- events -------------------------------------------------------------

$("#tabs").addEventListener("click", (e) => {
  const button = e.target.closest("button[data-view]");
  if (button) switchView(button.dataset.view);
});

$("#refresh").addEventListener("click", async (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  button.innerHTML = `<span class="spin"></span> Refreshing`;
  try {
    const result = await api("/api/refresh", { method: "POST" });
    toast(`${result.prices.quotes || 0} prices, ${result.buys} buys, ` +
          `${result.sells} sells, ${result.alerts} alerts`);
    await loadHealth();
    await viewToday(true);
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Refresh prices";
  }
});

document.addEventListener("click", async (e) => {
  const open = e.target.closest("[data-open-card]");
  if (open) return openCard(open.dataset.openCard);

  const unwatch = e.target.closest("[data-unwatch]");
  if (unwatch) {
    try {
      await api(`/api/watchlist/${encodeURIComponent(unwatch.dataset.unwatch)}` +
                `?variant=${encodeURIComponent(unwatch.dataset.variant)}`,
                { method: "DELETE" });
      toast("Removed from watchlist");
      viewWatchlist();
    } catch (err) { toast(err.message, true); }
    return;
  }

  const watchCard = e.target.closest("[data-watch-card]");
  if (watchCard) {
    try {
      await api("/api/watchlist", {
        method: "POST",
        body: JSON.stringify({ card_id: watchCard.dataset.watchCard, variant: "any" }),
      });
      toast("Added to watchlist");
    } catch (err) { toast(err.message, true); }
    return;
  }

  const sell = e.target.closest("[data-sell]");
  if (sell) {
    const max = sell.dataset.qty;
    const qty = prompt(`How many to sell? (1-${max})`, max);
    if (!qty) return;
    const price = prompt("Sale price per card");
    if (!price) return;
    try {
      const result = await api(`/api/holdings/${sell.dataset.sell}/sell`, {
        method: "POST",
        body: JSON.stringify({ quantity: Number(qty), price_each: Number(price) }),
      });
      toast(`Realised ${money(result.realized_pnl)} (${pct(result.roi, 0)})`);
      viewPortfolio();
    } catch (err) { toast(err.message, true); }
    return;
  }

  const drop = e.target.closest("[data-drop-lot]");
  if (drop) {
    if (!confirm("Delete this lot? This removes it without recording a sale.")) return;
    try {
      await api(`/api/holdings/${drop.dataset.dropLot}`, { method: "DELETE" });
      toast("Lot deleted");
      viewPortfolio();
    } catch (err) { toast(err.message, true); }
  }
});

$("#holding-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  try {
    await api("/api/holdings", {
      method: "POST",
      body: JSON.stringify({
        card_id: form.get("card_id").trim(),
        variant: form.get("variant").trim() || "normal",
        quantity: Number(form.get("quantity")),
        cost_each: Number(form.get("cost_each")),
        condition: form.get("condition").trim() || "NM",
      }),
    });
    toast("Holding added");
    e.target.reset();
    viewPortfolio();
  } catch (err) { toast(err.message, true); }
});

$("#watch-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const num = (key) => (form.get(key) ? Number(form.get(key)) : null);
  try {
    await api("/api/watchlist", {
      method: "POST",
      body: JSON.stringify({
        card_id: form.get("card_id").trim(),
        variant: form.get("variant").trim() || "any",
        max_buy: num("max_buy"),
        target_sell: num("target_sell"),
        note: form.get("note") || "",
      }),
    });
    toast("Watching");
    e.target.reset();
    viewWatchlist();
  } catch (err) { toast(err.message, true); }
});

$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const query = form.get("q").trim();
  const remote = form.get("remote") ? "&remote=true" : "";
  $("#search-results").innerHTML = `<div class="empty"><span class="spin"></span> Searching…</div>`;
  try {
    const rows = await api(`/api/cards/search?q=${encodeURIComponent(query)}${remote}`);
    $("#search-results").innerHTML = table(
      [
        { label: "Card", render: (r) => cardCell({ card_name: r.name, set_name: r.set_name,
                                                   number: r.number }) },
        { label: "Id", render: (r) => `<span class="meta">${esc(r.id)}</span>` },
        { label: "Rarity", render: (r) => esc(r.rarity || "") },
        { label: "", render: (r) =>
            `<button class="btn tiny" data-open-card="${esc(r.id)}">Open</button>
             <button class="btn tiny" data-watch-card="${esc(r.id)}">Watch</button>` },
      ],
      rows,
      "No matches. Tick 'search the provider' to pull new cards in."
    );
  } catch (err) {
    $("#search-results").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
});

$("#estimate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const setIds = (form.get("set_ids") || "")
    .split(",").map((s) => s.trim()).filter(Boolean);
  $("#estimate-result").innerHTML = `<div class="empty"><span class="spin"></span> Valuing…</div>`;
  try {
    const result = await api("/api/bulk/estimate", {
      method: "POST",
      body: JSON.stringify({
        card_count: Number(form.get("card_count")),
        set_ids: setIds,
        ask_price: form.get("ask_price") ? Number(form.get("ask_price")) : null,
        shipping: Number(form.get("shipping") || 0),
      }),
    });
    $("#estimate-result").innerHTML = renderEstimate(result);
  } catch (err) {
    $("#estimate-result").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
});

$("#lot-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const items = (form.get("items") || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [card_id, variant, quantity] = line.split(",").map((s) => (s || "").trim());
      return { card_id, variant: variant || "normal", quantity: Number(quantity || 1) };
    })
    .filter((i) => i.card_id);
  if (!items.length) return toast("Add at least one card", true);

  $("#lot-result").innerHTML = `<div class="empty"><span class="spin"></span> Valuing…</div>`;
  try {
    const result = await api("/api/bulk/value", {
      method: "POST",
      body: JSON.stringify({
        items,
        ask_price: form.get("ask_price") ? Number(form.get("ask_price")) : null,
        shipping: Number(form.get("shipping") || 0),
      }),
    });
    $("#lot-result").innerHTML = renderLotValuation(result);
  } catch (err) {
    $("#lot-result").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
});

$("#load-plan").addEventListener("click", async () => {
  $("#plan-result").innerHTML = `<div class="empty"><span class="spin"></span> Building…</div>`;
  try {
    const plan = await api("/api/bulk/sell-plan");
    $("#plan-result").innerHTML = `
      <div class="kpis">
        <div class="kpi"><div class="label">Singles net</div>
          <div class="value">${money(plan.singles_net_total)}</div>
          <div class="sub">${plan.sell_individually.length} lines worth listing</div></div>
        <div class="kpi"><div class="label">Bulk net</div>
          <div class="value">${money(plan.bulk_net_total)}</div>
          <div class="sub">${plan.bulk_card_count} cards</div></div>
        <div class="kpi"><div class="label">Suggested bulk ask</div>
          <div class="value up">${money(plan.suggested_bulk_ask)}</div>
          <div class="sub">leaves negotiating room</div></div>
      </div>
      <div class="split" style="margin-top:14px">
        <div>
          <div class="panel-head"><h2>Sell individually</h2></div>
          ${table(
            [
              { label: "Card", render: cardCell },
              { label: "Qty", num: true, render: (r) => r.quantity },
              { label: "Net ea", num: true, render: (r) => money(r.net_if_single) },
              { label: "Total", num: true, render: (r) => money(r.quantity_value_single) },
            ],
            plan.sell_individually.slice(0, 20)
          )}
        </div>
        <div>
          <div class="panel-head"><h2>Move as bulk</h2></div>
          ${table(
            [
              { label: "Card", render: cardCell },
              { label: "Qty", num: true, render: (r) => r.quantity },
              { label: "Net ea", num: true, render: (r) => money(r.net_if_bulk) },
              { label: "Why", cls: "reason",
              render: (r) => `<span class="meta">${esc(r.reason)}</span>` },
            ],
            plan.sell_as_bulk.slice(0, 20)
          )}
        </div>
      </div>`;
  } catch (err) {
    $("#plan-result").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
});

$("#ack-all").addEventListener("click", async () => {
  try {
    const result = await api("/api/alerts/ack", { method: "POST" });
    toast(`Marked ${result.acknowledged} read`);
    viewAlerts();
  } catch (err) { toast(err.message, true); }
});

// --- boot ---------------------------------------------------------------

loadHealth();
const initial = (location.hash || "").replace(/^#/, "");
switchView(initial in VIEWS ? initial : "today");

window.addEventListener("hashchange", () => {
  const name = location.hash.replace(/^#/, "");
  if (name in VIEWS && $(`#view-${name}`).classList.contains("hidden")) {
    switchView(name);
  }
});

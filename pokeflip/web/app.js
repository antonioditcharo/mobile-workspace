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
  if (!rows.length) {
    const [head, ...rest] = String(emptyText).split(" — ");
    return emptyState(head, rest.join(" — "));
  }
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

const cardCell = (r) => {
  const art = r.image
    ? `<img src="${esc(r.image)}" alt="" loading="lazy">`
    : "";
  return `<div class="cardcell">${art}<div>
    <div class="name">${esc(r.card_name || r.name || r.card_id)}</div>
    <div class="meta">${esc(r.set_name || "")} ${esc(r.number || "")}</div>
  </div></div>`;
};

/** A score reads faster as a chip than as a bare number. */
const scoreCell = (v) => {
  const tone = v >= 70 ? "high" : v >= 50 ? "mid" : "";
  return `<span class="score ${tone}">${v.toFixed(0)}</span>`;
};

const skeleton = (lines = 4) =>
  `<div class="skeleton">${'<div class="line"></div>'.repeat(lines)}</div>`;

const emptyState = (title, hint = "") =>
  `<div class="empty"><strong>${esc(title)}</strong>${hint ? esc(hint) : ""}</div>`;

// --- chart --------------------------------------------------------------


/**
 * Evenly spaced date labels that never collide.
 *
 * Labelling every Nth point *and* always labelling the last one puts two
 * labels on top of each other whenever the series length is not a multiple of
 * the step - so the last label wins and the one it would overlap is dropped.
 */
function dateLabels(dates, x, height, target = 7) {
  const every = Math.max(1, Math.floor(dates.length / target));
  const last = dates.length - 1;
  // In viewBox units; roughly the width of a "05-13" label.
  const minGap = 34;
  const chosen = [];
  for (let i = 0; i < dates.length; i += every) chosen.push(i);
  if (chosen[chosen.length - 1] !== last) {
    while (chosen.length && x(last) - x(chosen[chosen.length - 1]) < minGap) {
      chosen.pop();
    }
    chosen.push(last);
  }
  return chosen
    .map((i) => `<text class="axis-text" x="${x(i).toFixed(1)}" y="${height - 6}"
       text-anchor="middle">${dates[i].slice(5)}</text>`)
    .join("");
}

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

  const dateLabelMarkup = dateLabels(points.map((p) => p.on), x, H, 6);

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
      ${dateLabelMarkup}
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


/**
 * Portfolio value over time: one filled series for net liquidation, with cost
 * basis as a recessive dashed reference. The baseline is not a competing
 * series so it wears muted ink rather than a categorical hue, and the gap
 * between the two lines *is* the profit - which is the whole point.
 */
function portfolioChart(days) {
  const rows = days.filter((d) => d.net_value != null);
  if (rows.length < 2) {
    return emptyState("Not enough history to plot",
                      "Value is reconstructed from stored prices; give it a few days.");
  }

  const net = rows.map((d) => d.net_value);
  const cost = rows.map((d) => d.cost_basis);
  const W = 900, H = 240, ML = 58, MR = 14, MT = 12, MB = 24;
  const plotW = W - ML - MR, plotH = H - MT - MB;

  const all = [...net, ...cost];
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo) * 0.12 || hi * 0.1 || 1;
  lo = Math.max(0, lo - pad);
  hi += pad;

  const x = (i) => ML + (i / (rows.length - 1)) * plotW;
  const y = (v) => MT + plotH - ((v - lo) / (hi - lo || 1)) * plotH;
  const line = (vals) =>
    vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const area = `${line(net)}L${x(rows.length - 1).toFixed(1)},${(MT + plotH).toFixed(1)}`
             + `L${ML.toFixed(1)},${(MT + plotH).toFixed(1)}Z`;

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => lo + (hi - lo) * f);
  const grid = ticks.map((t) =>
    `<line class="grid-line" x1="${ML}" x2="${W - MR}" y1="${y(t).toFixed(1)}"
       y2="${y(t).toFixed(1)}"/>
     <text class="axis-text" x="${ML - 8}" y="${(y(t) + 3).toFixed(1)}"
       text-anchor="end">$${Math.round(t).toLocaleString("en-US")}</text>`).join("");

  const dates = dateLabels(rows.map((d) => d.on), x, H, 7);

  const id = `pf-${Math.random().toString(36).slice(2, 9)}`;
  const series = JSON.stringify(rows.map((d, i) => ({
    on: d.on, net: d.net_value, cost: d.cost_basis, pnl: d.unrealized,
    cards: d.cards, x: +x(i).toFixed(2),
    yn: +y(d.net_value).toFixed(2), yc: +y(d.cost_basis).toFixed(2),
  })));

  const last = rows[rows.length - 1];
  return `
  <div class="chart-wrap">
    <div class="legend">
      <span><i class="k1"></i>Net if sold <b>${money(last.net_value)}</b></span>
      <span><i class="kbase"></i>Cost basis <b>${money(last.cost_basis)}</b></span>
      <span class="${signClass(last.unrealized)}">Unrealised
        <b class="${signClass(last.unrealized)}">${money(last.unrealized)}</b></span>
    </div>
    <svg class="chart" id="${id}" viewBox="0 0 ${W} ${H}" role="img"
         aria-label="Portfolio net value and cost basis over time"
         data-kind="portfolio" data-series='${esc(series)}'>
      ${grid}${dates}
      <path class="area" d="${area}" fill="var(--series-1)" opacity="0.12"/>
      <path class="baseline" d="${line(cost)}"/>
      <path class="series s1" d="${line(net)}"/>
      <g class="focus" style="display:none">
        <line class="crosshair" y1="${MT}" y2="${MT + plotH}"/>
        <circle class="focus-dot d1" r="4" fill="var(--series-1)"/>
        <circle class="focus-dot d2" r="3.5" fill="var(--text-muted)"/>
      </g>
      <rect class="hit" x="${ML}" y="${MT}" width="${plotW}" height="${plotH}"/>
    </svg>
    <div class="tooltip hidden"></div>
  </div>`;
}

/** An inline trend, coloured by direction. Too small for axes, so it carries
 *  no numbers - the row beside it already has them. */
function sparkline(values) {
  const points = (values || []).filter((v) => v != null);
  if (points.length < 2) return `<span class="meta">-</span>`;
  const W = 72, H = 22, pad = 2;
  const lo = Math.min(...points), hi = Math.max(...points);
  const span = hi - lo || 1;
  const x = (i) => (i / (points.length - 1)) * W;
  const y = (v) => pad + (1 - (v - lo) / span) * (H - pad * 2);
  const d = points.map((v, i) =>
    `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const change = (points[points.length - 1] - points[0]) / (points[0] || 1);
  const tone = change > 0.02 ? "up" : change < -0.02 ? "down" : "flat";
  const fillTone = tone === "up" ? "var(--good)"
                 : tone === "down" ? "var(--bad)" : "var(--text-muted)";
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" aria-hidden="true">
    <path class="fill" d="${d}L${W},${H}L0,${H}Z" fill="${fillTone}"/>
    <path class="${tone}" d="${d}"/>
  </svg>`;
}

/** Shares compared against each other. A bar chart, never a pie. */
function allocationBars(rows, { limit = 6, overAt = null } = {}) {
  if (!rows.length) return emptyState("Nothing to break down yet");
  const top = rows.slice(0, limit);
  const max = Math.max(...top.map((r) => r.share), 0.0001);
  return `<div class="bars">${top.map((r) => {
    const over = overAt != null && r.share > overAt;
    return `<div class="bar-row">
      <div class="bar-label" title="${esc(r.label)}">${esc(r.label)}</div>
      <div class="bar-track">
        <div class="bar-fill ${over ? "over" : ""}"
             style="width:${Math.max(2, (r.share / max) * 100).toFixed(1)}%"></div>
      </div>
      <div class="bar-value">${(r.share * 100).toFixed(1)}%</div>
    </div>`;
  }).join("")}</div>`;
}

/** Wire hover behaviour for every chart currently in the DOM. */
function bindCharts(root = document) {
  $$(".chart", root).forEach((svg) => {
    if (svg.dataset.bound) return;
    svg.dataset.bound = "1";

    const series = JSON.parse(svg.dataset.series);
    const symbol = svg.dataset.sym || "$";
    const kind = svg.dataset.kind || "price";
    const wrap = svg.closest(".chart-wrap");
    const tip = $(".tooltip", wrap);
    const focus = $(".focus", svg);
    const hit = $(".hit", svg);

    // Each chart carries its own point shape, so the readout is defined
    // alongside it rather than assumed.
    const readouts = {
      price: (p) => [
        ["Market", `${symbol}${p.market.toFixed(2)}`, "var(--series-1)"],
        ["7-day", `${symbol}${p.sma7.toFixed(2)}`, "var(--series-2)"],
        ["30-day", `${symbol}${p.sma30.toFixed(2)}`, "var(--series-3)"],
      ],
      portfolio: (p) => [
        ["Net if sold", money(p.net), "var(--series-1)"],
        ["Cost basis", money(p.cost), "var(--text-muted)"],
        ["Unrealised", money(p.pnl), null],
        [`${p.cards} cards`, "", null],
      ],
    };
    const dots = kind === "portfolio"
      ? [[".d1", "yn"], [".d2", "yc"]]
      : [[".d1", "ym"], [".d2", "y7"], [".d3", "y30"]];

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
      dots.forEach(([sel, key]) => {
        const dot = $(sel, focus);
        if (!dot) return;
        dot.setAttribute("cx", nearest.x);
        dot.setAttribute("cy", nearest[key]);
      });

      const rows = (readouts[kind] || readouts.price)(nearest);
      tip.innerHTML = `<div class="t-date">${esc(nearest.on)}</div>` + rows
        .map(([label, value, swatch]) =>
          `<div class="t-row"><span>${
            swatch ? `<i style="background:${swatch}"></i>` : ""}${esc(label)}</span>` +
          `<b>${esc(value)}</b></div>`)
        .join("");
      tip.classList.remove("hidden");

      const scale = box.width / svg.viewBox.baseVal.width;
      const left = nearest.x * scale;
      const flip = left > box.width - 160;
      const anchor = kind === "portfolio" ? nearest.yn : nearest.ym;
      tip.style.left = `${flip ? left - tip.offsetWidth - 12 : left + 12}px`;
      tip.style.top = `${Math.max(0, anchor * scale - 10)}px`;
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
    $("#action-list").innerHTML = `<li>${skeleton(3)}</li>`;
    $("#today-chart").innerHTML = skeleton(5);
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
          (a, i) => `<li>
            <span class="idx">${i + 1}</span>
            <div>
              <div class="head"><span class="pill ${esc(a.type)}">${esc(a.type)}</span>
                <span class="name">${esc(a.card)}</span></div>
              <div class="detail">${esc(a.detail)}</div>
              ${a.why ? `<div class="why">${esc(a.why)}</div>` : ""}
            </div>
          </li>`
        )
        .join("")
    : `<li style="display:block">${emptyState(
        "Nothing clears your thresholds today",
        "Sitting out is a position.")}</li>`;

  api("/api/portfolio/history?days=90")
    .then((h) => { $("#today-chart").innerHTML = portfolioChart(h.days || []);
                   bindCharts($("#today-chart")); })
    .catch(() => { $("#today-chart").innerHTML = emptyState("Could not load history"); });

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
      { label: "Score", num: true, render: (r) => scoreCell(r.score) },
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
      { label: "Score", num: true, render: (r) => scoreCell(r.score) },
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
  $("#pf-chart").innerHTML = skeleton(5);
  api("/api/portfolio/history?days=180")
    .then((h) => { $("#pf-chart").innerHTML = portfolioChart(h.days || []);
                   bindCharts($("#pf-chart")); })
    .catch(() => { $("#pf-chart").innerHTML = emptyState("Could not load history"); });

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
      { label: "Trend", render: (r) => sparkline(r.spark) },
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

// --- orders & listings --------------------------------------------------

const VERDICT_CLASS = { cut: "alert", raise: "buy", pull: "sell", hold: "" };

async function viewOrders() {
  const [health, book, risk] = await Promise.all([
    api("/api/listings"),
    api("/api/orders?status=open"),
    api("/api/portfolio/concentration"),
  ]);

  const buys = book.filter((o) => o.kind === "buy");
  $("#order-kpis").innerHTML = `
    <div class="kpi"><div class="label">Open bids</div>
      <div class="value">${buys.length}</div>
      <div class="sub">${money(buys.reduce((a, o) => a + (o.limit_price || 0) * o.quantity, 0))} committed</div></div>
    <div class="kpi"><div class="label">Live listings</div>
      <div class="value">${health.count}</div>
      <div class="sub">${money(health.listed_value)} at ask</div></div>
    <div class="kpi"><div class="label">Need a decision</div>
      <div class="value ${health.needs_action.length ? "down" : ""}">${health.needs_action.length}</div>
      <div class="sub">${health.stale_count} stale</div></div>
    <div class="kpi"><div class="label">Free capital</div>
      <div class="value">${risk.free_capital == null ? "-" : money(risk.free_capital)}</div>
      <div class="sub">${risk.bankroll ? `of ${money(risk.bankroll)}` : "set capital.bankroll"}</div></div>`;

  $("#listings-table").innerHTML = table(
    [
      { label: "Card", render: cardCell },
      { label: "Qty", num: true, render: (r) => r.quantity },
      { label: "Ask", num: true, render: (r) => money(r.limit_price) },
      { label: "Market", num: true, render: (r) => money(r.market_price) },
      { label: "vs mkt", num: true,
        render: (r) => `<span class="${signClass(-(r.ask_vs_market ?? 0))}">${pct(r.ask_vs_market, 0)}</span>` },
      { label: "Days", num: true, render: (r) => r.days_on_market ?? "-" },
      { label: "At price", num: true, render: (r) => r.days_at_price ?? "-" },
      { label: "Verdict", render: (r) =>
          `<span class="pill ${VERDICT_CLASS[r.verdict] || ""}">${esc(r.verdict)}</span>` },
      { label: "Suggest", num: true, render: (r) => money(r.suggested_price) },
      { label: "Why", cls: "reason",
        render: (r) => `<span class="meta">${esc(r.reason || "")}</span>` },
      { label: "", render: (r) => r.suggested_price
          ? `<button class="btn tiny" data-reprice="${r.id}"
               data-price="${r.suggested_price}">Apply</button>` : "" },
    ],
    health.listings,
    "Nothing listed. Record a listing below, or take a sell recommendation from Today."
  );

  const rows = await api(`/api/orders?status=${$("#orders-all").checked ? "" : "open"}`);
  $("#orders-table").innerHTML = table(
    [
      { label: "#", num: true, render: (r) => r.id },
      { label: "Kind", render: (r) => `<span class="pill ${r.kind}">${esc(r.kind)}</span>` },
      { label: "Status", render: (r) => esc(r.status) },
      { label: "Card", render: cardCell },
      { label: "Qty", num: true, render: (r) => r.quantity },
      { label: "Price", num: true, render: (r) => money(r.limit_price) },
      { label: "Cond", render: (r) => esc(r.condition) },
      { label: "Signal", render: (r) => `<span class="meta">${esc(r.signal_action || "")}</span>` },
      { label: "", render: (r) => r.status !== "open" ? "" :
          `<button class="btn tiny" data-fill="${r.id}" data-qty="${r.quantity}"
             data-price="${r.limit_price ?? ""}">Filled</button>
           <button class="btn tiny" data-cancel-order="${r.id}">Cancel</button>` },
    ],
    rows,
    "No orders yet."
  );

  // Shares are a comparison, so they get bars. Anything past the configured
  // limit is painted with the bad tone rather than left for you to spot.
  const positionLimit = 0.20, setLimit = 0.40;
  $("#risk-panel").innerHTML = `
    ${risk.warnings.length
      ? `<ul class="notes">${risk.warnings.map((w) =>
          `<li class="down">${esc(w.message)}</li>`).join("")}</ul>`
      : `<div class="meta" style="margin-bottom:12px">No concentration limits exceeded.</div>`}
    <div class="split" style="margin-top:12px">
      <div>
        <div class="panel-head"><h2>Largest positions</h2></div>
        ${allocationBars(
          risk.top_positions.map((r) => ({
            label: `${r.card_name}${r.condition && r.condition !== "NM"
              ? ` (${r.condition})` : ""}`,
            share: r.share,
          })), { overAt: positionLimit })}
      </div>
      <div>
        <div class="panel-head"><h2>By set</h2></div>
        ${allocationBars(
          risk.by_set.map((r) => ({ label: r.set_name, share: r.share })),
          { overAt: setLimit })}
      </div>
    </div>`;
}

// --- grading ------------------------------------------------------------

function renderGrading(v) {
  const cls = { grade: "up", marginal: "", sell_raw: "down" }[v.verdict] || "";
  return `
    <div class="kpis" style="margin-top:12px">
      <div class="kpi"><div class="label">Raw price</div>
        <div class="value">${money(v.raw_price)}</div>
        <div class="sub">${money(v.raw_net)} net if sold raw</div></div>
      <div class="kpi"><div class="label">Expected net</div>
        <div class="value">${money(v.expected_net)}</div>
        <div class="sub">after ${money(v.grading_cost)} of fees</div></div>
      <div class="kpi"><div class="label">Expected gain</div>
        <div class="value ${signClass(v.expected_profit)}">${money(v.expected_profit)}</div>
        <div class="sub">${v.expected_roi == null ? "" : pct(v.expected_roi, 0)} over selling raw</div></div>
      <div class="kpi"><div class="label">Verdict</div>
        <div class="value ${cls}">${esc(v.verdict.replace(/_/g, " ").toUpperCase())}</div>
        <div class="sub">${v.turnaround_days}d tied up &middot; basis: ${esc(v.basis)}</div></div>
    </div>
    <div style="margin-top:12px">${table(
      [
        { label: "Grade", render: (r) => esc(r.grade) },
        { label: "Odds", num: true, render: (r) => `${(r.probability * 100).toFixed(0)}%` },
        { label: "Price", num: true, render: (r) => money(r.price) },
        { label: "Net", num: true, render: (r) => money(r.net_proceeds) },
        { label: "Source", render: (r) => `<span class="pill ${r.source === "comp" ? "buy" : ""}">${esc(r.source)}</span>` },
      ],
      v.outcomes
    )}</div>
    <ul class="notes">${(v.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`;
}

async function viewGrading() { /* form-driven */ }

// --- scorecard ----------------------------------------------------------

const VERDICT_TONE = {
  reliable: "up", positive: "up", mixed: "", unreliable: "down",
  insufficient_data: "muted",
};

function renderScorecard(report) {
  $("#scorecard-table").innerHTML = table(
    [
      { label: "Kind", render: (r) => `<span class="pill ${r.kind}">${esc(r.kind)}</span>` },
      { label: "Reason", render: (r) => `<span class="name">${esc(r.action)}</span>` },
      { label: "Horizon", num: true, render: (r) => `${r.horizon_days}d` },
      { label: "Signals", num: true, render: (r) => r.signals },
      { label: "Graded", num: true, render: (r) => r.resolved },
      { label: "Win rate", num: true, render: (r) => {
          if (r.win_rate == null) return "-";
          const tone = r.win_rate >= 0.6 ? "var(--good)"
                     : r.win_rate >= 0.45 ? "var(--warn)" : "var(--bad)";
          return `<div style="display:flex;align-items:center;gap:6px;
                    justify-content:flex-end">
            <div class="bar-track" style="width:52px">
              <div class="bar-fill" style="width:${(r.win_rate * 100).toFixed(0)}%;
                   background:${tone}"></div>
            </div><span>${(r.win_rate * 100).toFixed(0)}%</span></div>`;
        } },
      { label: "Median ROI", num: true,
        render: (r) => `<span class="${signClass(r.median_roi)}">${pct(r.median_roi)}</span>` },
      { label: "Median move", num: true, render: (r) => pct(r.median_return) },
      { label: "Avg drawdown", num: true, render: (r) => pct(r.avg_max_adverse) },
      { label: "Verdict", render: (r) =>
          `<span class="${VERDICT_TONE[r.verdict] ?? ""}">${esc(r.verdict.replace(/_/g, " "))}</span>` },
    ],
    report.stats || [],
    "No scorecard yet — replay history to build one."
  );
  $("#scorecard-notes").innerHTML =
    (report.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
}

async function viewScorecard() {
  try {
    renderScorecard(await api("/api/backtest"));
  } catch (err) {
    $("#scorecard-table").innerHTML =
      `<div class="empty">No backtest stored yet. Hit "Replay history".</div>`;
    $("#scorecard-notes").innerHTML = "";
  }
}

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
  orders: viewOrders,
  cards: viewCards,
  bulk: viewBulk,
  grading: viewGrading,
  scorecard: viewScorecard,
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

// --- orders, grading, scorecard events ---------------------------------

document.addEventListener("click", async (e) => {
  const fill = e.target.closest("[data-fill]");
  if (fill) {
    const max = fill.dataset.qty;
    const qty = prompt(`How many filled? (1-${max})`, max);
    if (!qty) return;
    const price = prompt("Actual price per card", fill.dataset.price || "");
    if (!price) return;
    try {
      const result = await api(`/api/orders/${fill.dataset.fill}/fill`, {
        method: "POST",
        body: JSON.stringify({ quantity: Number(qty), price: Number(price) }),
      });
      toast(result.kind === "buy"
        ? `Bought ${result.quantity} — holding created`
        : `Realised ${money(result.realized_pnl)} (${pct(result.roi, 0)})`);
      viewOrders();
    } catch (err) { toast(err.message, true); }
    return;
  }

  const cancel = e.target.closest("[data-cancel-order]");
  if (cancel) {
    try {
      await api(`/api/orders/${cancel.dataset.cancelOrder}`, { method: "DELETE" });
      toast("Order cancelled");
      viewOrders();
    } catch (err) { toast(err.message, true); }
    return;
  }

  const rep = e.target.closest("[data-reprice]");
  if (rep) {
    const price = prompt("New asking price", rep.dataset.price);
    if (!price) return;
    try {
      await api(`/api/orders/${rep.dataset.reprice}/reprice`, {
        method: "POST",
        body: JSON.stringify({ price: Number(price), reason: "manual" }),
      });
      toast("Re-priced");
      viewOrders();
    } catch (err) { toast(err.message, true); }
  }
});

$("#orders-all").addEventListener("change", () => viewOrders());

$("#apply-cuts").addEventListener("click", async () => {
  if (!confirm("Re-price every listing flagged for a cut?")) return;
  try {
    const result = await api("/api/listings/apply-suggestions?verdicts=cut",
                             { method: "POST" });
    toast(`Re-priced ${result.applied.length} listing(s)`);
    viewOrders();
  } catch (err) { toast(err.message, true); }
});

$("#order-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  try {
    await api("/api/orders", {
      method: "POST",
      body: JSON.stringify({
        kind: form.get("kind"),
        card_id: form.get("card_id").trim(),
        variant: form.get("variant").trim() || "normal",
        condition: form.get("condition").trim() || "NM",
        quantity: Number(form.get("quantity")),
        limit_price: Number(form.get("limit_price")),
      }),
    });
    toast("Order recorded");
    e.target.reset();
    viewOrders();
  } catch (err) { toast(err.message, true); }
});

$("#grading-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  $("#grading-result").innerHTML = `<div class="empty"><span class="spin"></span> Evaluating…</div>`;
  try {
    const params = new URLSearchParams({
      variant: form.get("variant").trim() || "normal",
      condition: form.get("condition").trim() || "NM",
    });
    const verdict = await api(
      `/api/grading/${encodeURIComponent(form.get("card_id").trim())}?${params}`);
    $("#grading-result").innerHTML = renderGrading(verdict);
  } catch (err) {
    $("#grading-result").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
});

$("#comp-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  try {
    await api("/api/grading/comps", {
      method: "POST",
      body: JSON.stringify({
        card_id: form.get("card_id").trim(),
        grade: form.get("grade").trim(),
        price: Number(form.get("price")),
        variant: form.get("variant").trim() || "normal",
        service: form.get("service").trim() || "PSA",
      }),
    });
    toast("Comp recorded — it will replace the guessed multiplier");
    e.target.reset();
  } catch (err) { toast(err.message, true); }
});

$("#scan-grading").addEventListener("click", async () => {
  const panel = $("#grading-scan-panel");
  panel.classList.remove("hidden");
  $("#grading-scan").innerHTML = `<div class="empty"><span class="spin"></span> Scanning…</div>`;
  try {
    const scan = await api("/api/grading/scan");
    $("#grading-scan").innerHTML = table(
      [
        { label: "Card", render: cardCell },
        { label: "Raw", num: true, render: (r) => money(r.raw_price) },
        { label: "Exp. net", num: true, render: (r) => money(r.expected_net) },
        { label: "Gain", num: true,
          render: (r) => `<span class="${signClass(r.expected_profit)}">${money(r.expected_profit)}</span>` },
        { label: "Verdict", render: (r) =>
            `<span class="pill ${r.verdict === "grade" ? "buy" : r.verdict === "sell_raw" ? "sell" : "alert"}">${
              esc(r.verdict.replace(/_/g, " "))}</span>` },
        { label: "Basis", render: (r) => `<span class="meta">${esc(r.basis)}</span>` },
      ],
      scan.candidates,
      "Nothing you hold clears the grading price floor."
    ) + `<ul class="notes"><li>${esc(scan.note)}</li>
         <li>${scan.recommended.length} worth submitting: ${money(scan.submission_cost)}
         in fees for ${money(scan.total_expected_profit)} of expected upside.</li></ul>`;
  } catch (err) {
    $("#grading-scan").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
});

$("#run-backtest").addEventListener("click", async (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  button.innerHTML = `<span class="spin"></span> Replaying`;
  try {
    renderScorecard(await api("/api/backtest/run", { method: "POST" }));
    toast("Scorecard rebuilt");
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Replay history";
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

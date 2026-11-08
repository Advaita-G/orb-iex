/* Paper journal: read-only client for the ORB bot's local journal API.
 * It only fetches /api/*. External chart links send the symbol only when clicked.
 * A value that is unavailable is written as "n/a" (with the reason where known); it is never shown as 0.
 */
"use strict";

const TZ = "America/New_York";
const NA = "n/a";
const $ = (sel, root) => (root || document).querySelector(sel);

/* ------------------------------------------------------------------ format */
function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function n(v) {
  if (v === null || v === undefined || v === "") return null;
  const x = Number(v);
  return isFinite(x) ? x : null;
}
function fmt(x, digits) {
  return x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function usd(v, opts) {
  const x = n(v);
  if (x === null) return NA;
  const o = opts || {};
  const body = "$" + fmt(Math.abs(x), o.digits === undefined ? 2 : o.digits);
  if (x < 0) return "−" + body;
  return (o.sign && x > 0 ? "+" : "") + body;
}
function rmult(v) {
  const x = n(v);
  if (x === null) return NA;
  return (x < 0 ? "−" : x > 0 ? "+" : "") + fmt(Math.abs(x), 2) + "R";
}
function pct(v) {   // ratio 0..1
  const x = n(v);
  return x === null ? NA : Math.round(x * 100) + "%";
}
function tone(v) {
  const x = n(v);
  return x === null || x === 0 ? "" : x > 0 ? "pos" : "neg";
}
function plural(k, word, many) { return `${k} ${k === 1 ? word : (many || word + "s")}`; }
function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }
function etTime(ts) {
  if (!ts) return NA;
  const d = new Date(ts);
  if (isNaN(d)) return NA;
  return d.toLocaleTimeString("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
}
function dayDate(day) { return new Date(day + "T12:00:00Z"); }
function longDay(day) {
  return dayDate(day).toLocaleDateString("en-US", { timeZone: "UTC", weekday: "long", month: "long", day: "numeric" });
}
function shortDay(day) {
  return dayDate(day).toLocaleDateString("en-US", { timeZone: "UTC", month: "short", day: "numeric" });
}
function monthName(m) {
  return dayDate(m + "-15").toLocaleDateString("en-US", { timeZone: "UTC", month: "long", year: "numeric" });
}
function held(sec) {
  const s = n(sec);
  if (s === null) return NA;
  if (s < 60) return `${Math.round(s)} sec`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${fmt(s / 3600, 1)} hr`;
}
function sideWord(side) { return side === "short" ? "Short" : side === "long" ? "Long" : cap(side || NA); }

/* ------------------------------------------------------------------ data */
async function api(path, params) {
  const q = params ? "?" + new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v !== undefined && v !== null)) : "";
  const res = await fetch(path + q, { headers: { Accept: "application/json" } });
  let body;
  try {
    body = await res.json();
  } catch (err) {
    throw new Error(`${path} returned something that is not JSON (HTTP ${res.status})`);
  }
  if (!res.ok || body.error) throw new Error(body.error || `${path} failed with HTTP ${res.status}`);
  return body;
}

/* What the journal says about one loss: the daily review if it exists, else the classifier's suggestion. */
const LOSS_WORDS = {
  normal_strategy: "normal stop-out",
  execution_defect: "the bot made an execution error",
  data_issue: "bad or missing market data",
  broker_issue: "a broker problem",
  mixed: "several problems at once",
  unknown: "not enough record to judge",
};
function judgement(trade, review) {
  const pnl = n(trade.realized_pnl);
  if (n(trade.open_qty) > 0 && pnl === null) return { kind: "open", word: "Still open", why: `stop at ${trade.final_stop ?? NA}` };
  if (review) {
    if (review.verdict === "unknown") return { kind: "unknown", word: "Unknown", why: review.title };
    return review.verdict === "mistake"
      ? { kind: "mistake", word: review.era === "pre-hardening" ? "Old bot mistake" : "Bot mistake", why: review.title }
      : { kind: "unlucky", word: "Bad luck", why: review.title };
  }
  if (pnl !== null && pnl >= 0) return { kind: "unlucky", word: pnl > 0 ? "Win" : "Break-even", why: "" };
  const suggested = trade.loss_class === "normal_strategy" ? "Bad luck?" : "Mistake?";
  return { kind: "pending", word: suggested, why: "", title: `Not reviewed yet. First guess: ${LOSS_WORDS[trade.loss_class] || trade.loss_class}` };
}
function verdict(j) {
  return `<span class="verdict ${j.kind}"${j.title ? ` title="${esc(j.title)}"` : ""}><b>${esc(j.word)}</b>${j.why ? esc(j.why) : ""}</span>`;
}

/* The bot's state in plain words, from /api/health. */
const BREAKERS = {
  overnight_position: ["Position might be held overnight", "Set when the bot thinks it holds shares outside market hours. You clear this one by hand after checking the position."],
  auto_safety_failure: ["A safety check failed while trading", "Clears itself after several fresh healthy checks, once no manual stop is on."],
  auto_clock_skew: ["Clock check failed", "The Mac's clock and the broker's clock looked more than 2 seconds apart. Clears itself after fresh checks."],
  auto_market_closed: ["Broker said the market was closed", "Clears itself once the broker reports the session again."],
  auto_control_failure: ["Could not reach the broker's controls", "Clears itself after fresh checks."],
  auto_broker_failure: ["Broker error", "Clears itself after fresh checks."],
  auto_calendar_failure: ["Market calendar unavailable", "Clears itself after fresh checks."],
  auto_data_failure: ["Opening data failed", "Clears when the next session starts."],
  unprotected_position: ["A position had no stop", "You clear this one by hand after checking the position."],
  duplicate_order: ["More than one stop for a symbol", "You clear this one by hand."],
  stale_market_data: ["Market data went stale", "You clear this one by hand."],
  sign_flip: ["A position flipped from long to short", "You clear this one by hand."],
};
function breakerWords(b) { return BREAKERS[b.kind] || [b.kind.replace(/_/g, " "), b.detail || ""]; }

function botState(health) {
  const st = health.status;
  const rr = (health.safety && health.safety.runtime_report) || {};
  const hb = st.heartbeat_state;
  const stops = st.breakers || [];
  if (hb === "missing" || hb === "dead" || hb === "stale") {
    const why = { missing: "there is no heartbeat file", dead: "its process is gone", stale: "its heartbeat went silent" }[hb];
    return { cls: "bad", short: "Not running", sentence: `The bot is not running: ${why}.` };
  }
  const release = (st.release_status || "").toUpperCase();
  if (release === "FAILED CLOSED") {
    const t = (rr.transitions || []).filter(x => x.phase === "FAILED CLOSED").pop();
    const when = t ? ` at ${etTime(t.at)} ET` : "";
    return { cls: "warn", short: "Paused", sentence: `The bot paused itself${when}. ${plural(stops.length, "safety stop")} ${stops.length === 1 ? "is" : "are"} on, so it won't open new trades.` };
  }
  if (stops.length) return { cls: "warn", short: "Running, limited", sentence: `The bot is running with ${plural(stops.length, "safety stop")} on.` };
  const words = {
    TRADING: "Trading", WAITING: "Waiting for the open", "READY FOR SCAN": "Ready for the opening scan",
    RECOVERING: "Recovering after a restart", DONE: "Done for the day", CLOSED: "Market closed",
  };
  const short = words[release] || cap((st.release_status || st.phase || "running").toLowerCase());
  return { cls: "ok", short, sentence: `The bot is running. ${short}.` };
}

/* ------------------------------------------------------------------ chart */
const CHARTS = [];
function chartSlot(points, key, opts) {
  CHARTS.push({ points, key, ...(opts || {}) });
  return `<div class="chart-slot" data-chart="${CHARTS.length - 1}"></div>`;
}
function drawCharts(root) {
  (root || document).querySelectorAll(".chart-slot").forEach(el => {
    const c = CHARTS[Number(el.dataset.chart)];
    if (!c) return;
    const w = Math.max(240, Math.round(el.clientWidth));
    el.innerHTML = c.bars ? barChart(c.points, c.key, w) : lineChart(c.points, c.key, w, c.key2);
  });
}
function lineChart(points, key, width, key2) {
  const rows = points.filter(p => n(p[key]) !== null);
  const vals = rows.map(p => n(p[key]));
  if (vals.length < 2) return `<p class="empty">Not enough trading days for a chart yet.</p>`;
  const vals2 = key2 ? rows.map(p => n(p[key2])) : [];
  const W = width || 720, H = Math.round(Math.max(170, Math.min(280, W * 0.42))), L = 4, R = 74, T = 14, B = 26;
  const all = vals.concat(vals2.filter(v => v !== null));
  const lo = Math.min(0, ...all), hi = Math.max(0, ...all);
  const span = hi - lo || 1;
  const x = i => L + (i * (W - L - R)) / (vals.length - 1);
  const y = v => T + ((hi - v) * (H - T - B)) / span;
  const trace = vs => vs.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v === null ? 0 : v).toFixed(1)}`).join("");
  const path = trace(vals);
  const area = `${path}L${x(vals.length - 1).toFixed(1)},${y(0).toFixed(1)}L${x(0).toFixed(1)},${y(0).toFixed(1)}Z`;
  const last = vals[vals.length - 1];
  const first = rows[0].day, end = rows[rows.length - 1].day;
  const last2 = key2 ? vals2[vals2.length - 1] : null;
  const label2 = last2 === null ? "" : `<text class="end2" x="${x(vals.length - 1) + 10}" y="${y(last2) + (Math.abs(y(last2) - y(last)) < 15 ? 16 : 4)}">${esc(usd(last2, { digits: 0 }))}</text>`;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Running total from ${esc(shortDay(first))} to ${esc(shortDay(end))}, ending at ${esc(usd(last))}${last2 === null ? "" : ` (${esc(usd(last2))} after costs)`}">
    <line class="zero" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>
    <path class="area ${last >= 0 ? "up" : ""}" d="${area}"/>
    ${key2 ? `<path class="line2" d="${trace(vals2)}"/>` : ""}
    <path class="line" d="${path}"/>
    <circle cx="${x(vals.length - 1)}" cy="${y(last)}" r="3.5"/>
    <text class="end" x="${x(vals.length - 1) + 10}" y="${y(last) + 4}">${esc(usd(last, { digits: 0 }))}</text>
    ${label2}
    <text x="${x(vals.length - 1) + 10}" y="${y(0) + (Math.abs(y(0) - y(last)) < 15 ? 18 : 4)}">$0</text>
    <text x="${L}" y="${H - 6}">${esc(shortDay(first))}</text>
    <text x="${W - R}" y="${H - 6}" text-anchor="end">${esc(shortDay(end))}</text>
  </svg>`;
}
/* One bar per trading day, green above zero and red below. */
function barChart(points, key, width) {
  const rows = points.filter(p => n(p[key]) !== null);
  if (rows.length < 1) return `<p class="empty">No trading days yet.</p>`;
  const W = width || 720, H = 200, T = 16, B = 28, L = 4, R = 4;
  const vals = rows.map(p => n(p[key]));
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = hi - lo || 1;
  const y = v => T + ((hi - v) * (H - T - B)) / span;
  const slot = (W - L - R) / rows.length, bw = Math.max(4, Math.min(48, slot * 0.66));
  const bars = rows.map((p, i) => {
    const v = vals[i], cx = L + slot * i + slot / 2;
    const top = Math.min(y(v), y(0)), h = Math.max(2, Math.abs(y(v) - y(0)));
    const labelY = v >= 0 ? top - 4 : top + h + 12;
    return `<g><title>${esc(shortDay(p.day))}: ${esc(usd(v))}</title>
      <rect class="${v >= 0 ? "up" : "down"}" x="${(cx - bw / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="2"/>
      ${slot >= 44 ? `<text class="bv" x="${cx.toFixed(1)}" y="${labelY.toFixed(1)}" text-anchor="middle">${esc(usd(v, { digits: 0 }))}</text>` : ""}
      ${slot >= 30 || i % Math.ceil(30 / slot) === 0 ? `<text x="${cx.toFixed(1)}" y="${H - 8}" text-anchor="middle">${esc(shortDay(p.day).replace(/^\w+ /, ""))}</text>` : ""}</g>`;
  }).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Realized result for each of ${rows.length} trading days"><line class="zero" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>${bars}</svg>`;
}

/* ------------------------------------------------------------------ router */
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "") || "overview";
  const [path, qs] = raw.split("?");
  const parts = path.split("/");
  return { view: parts[0] || "overview", arg: parts.slice(1).map(decodeURIComponent).join("/"), params: Object.fromEntries(new URLSearchParams(qs || "")) };
}
function href(view, params) {
  const q = new URLSearchParams(Object.entries(params || {}).filter(([, v]) => v !== "" && v !== undefined && v !== null)).toString();
  return `#/${view}${q ? "?" + q : ""}`;
}
function stockChart(symbol, label = symbol) {
  const url = `https://www.tradingview.com/chart/zOWgj5EF/?symbol=${encodeURIComponent(symbol)}&interval=5`;
  return `<a class="chart-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="Independent TradingView 5-minute chart; symbol only, no fills or P&amp;L">${esc(label)} ↗</a>`;
}

const cache = { health: null, overview: null, trades: [] };
async function overviewData(refresh) {
  if (!cache.overview || refresh) {
    const [ov, all] = await Promise.all([api("/api/overview"), api("/api/trades", { sort: "realized_pnl", order: "desc", limit: 5000 })]);
    cache.overview = ov;
    cache.trades = all.rows;
  }
  return cache.overview;
}
async function health(refresh) {
  if (!cache.health || refresh) cache.health = await api("/api/health");
  return cache.health;
}

/* ------------------------------------------------------------------ layout */
/* Every page is an ink band (what it is, in one line) over a paper body (the evidence). */
function band(inner, cls) {
  return `<section class="band${cls ? " " + cls : ""}"><div class="wrap">${inner}</div></section>`;
}
function body(inner) {
  return `<div class="page enter"><div class="wrap">${inner}</div></div>`;
}
function secHead(title, sub, id) {
  return `<div class="sec-head"><h2 class="h2"${id ? ` id="${id}"` : ""}>${esc(title)}</h2>${sub ? `<p>${sub}</p>` : ""}</div>`;
}
function catWords(c) { return cap(String(c || "").replace(/[-_]/g, " ")); }

/* What the bot learned: one line per kind of mistake, from the loss journal. */
function lessonRows(lessons) {
  return lessons.map(l => {
    const [cls, word] = l.recurred_after_fix ? ["bad", "Came back after a fix"]
      : l.shipped_on ? ["ok", l.tests.length ? "Fixed, a test guards it" : "Fixed"]
      : ["wait", "Not fixed yet"];
    const last = l.days[l.days.length - 1];
    return `<div class="lesson"><span class="times">${l.times}×</span><span class="k">${esc(catWords(l.category))}</span><span class="s ${cls}">${esc(word)}</span>
      <span class="t">${esc(l.titles.join("; "))}. Last seen ${esc(shortDay(last))}.</span></div>`;
  }).join("");
}

/* ------------------------------------------------------------------ totals strip (every page) */
function scoreCell(label, value, sub, cls) {
  return `<div class="sc"><dt>${esc(label)}</dt><dd class="${cls || ""}">${value}</dd>${sub ? `<small>${esc(sub)}</small>` : ""}</div>`;
}
function scoreHtml(ov) {
  const a = ov.periods && ov.periods.all_time;
  const td = ov.periods && ov.periods.today;
  if (!a || !a.trades) return `<div class="wrap"><p class="small">No closed trades yet. The totals appear after the first trading day.</p></div>`;
  const eq = ov.equity || {};
  const pts = eq.points || [];
  const since = pts.length ? shortDay(pts[0].day) : "";
  const open = (ov.positions && ov.positions.open_count) || 0;
  const hasToday = td && td.trades;
  const ts = tradeStats(cache.trades);
  return `<div class="wrap"><dl class="score-grid">
    ${scoreCell("Total P&L", esc(usd(a.realized_pnl, { sign: true })), `since ${since}, ${plural(a.trading_days, "session")}`, "lead " + tone(a.realized_pnl))}
    ${scoreCell("After costs", esc(usd(a.modeled_net_pnl, { sign: true })), `${usd(a.cost_per_trade)} cost a trade`, tone(a.modeled_net_pnl))}
    ${scoreCell("Today", hasToday ? esc(usd(td.realized_pnl, { sign: true })) : "—", hasToday ? plural(td.trades, "trade") : "no trades closed", hasToday ? tone(td.realized_pnl) : "")}
    ${scoreCell("Win rate", esc(pct(ts.winRate)), `${ts.wins} won, ${ts.losses} lost of ${a.trades}`)}
    ${scoreCell("Profit factor", esc(ts.profitFactor === null ? NA : fmt(ts.profitFactor, 2)), "1.00 is break-even")}
    ${scoreCell("Average trade", esc(rmult(a.expectancy_r)), `${usd(a.expectancy_per_trade, { sign: true })} per trade`, tone(a.expectancy_r))}
    ${scoreCell("Deepest drop", esc(usd(eq.max_drawdown)), "worst peak to trough")}
    ${scoreCell("Open now", esc(String(open)), open ? plural(open, "position") : "all flat")}
  </dl></div>`;
}
async function paintScore() {
  const el = $("#score");
  try {
    el.innerHTML = scoreHtml(await overviewData(true));
  } catch (err) {
    el.innerHTML = `<div class="wrap"><p class="small">Totals unavailable: ${esc(err.message)}</p></div>`;
  }
}

/* ------------------------------------------------------------------ Overview */
function dayStats(pts) {
  const d = pts.map(p => ({ day: p.day, v: n(p.daily_realized) })).filter(x => x.v !== null);
  if (!d.length) return null;
  const best = d.reduce((a, b) => (b.v > a.v ? b : a)), worst = d.reduce((a, b) => (b.v < a.v ? b : a));
  const sign = v => (v > 0 ? 1 : v < 0 ? -1 : 0);
  let run = 0;
  for (let i = d.length - 1; i >= 0 && sign(d[i].v) === sign(d[d.length - 1].v); i--) run++;
  return { best, worst, green: d.filter(x => x.v > 0).length, red: d.filter(x => x.v < 0).length, run, runSign: sign(d[d.length - 1].v), count: d.length };
}
/* Best, worst and average win/loss from the trade rows themselves, in fill-price dollars, so every number on a page uses the same basis as the headline total. */
function tradeStats(rows) {
  const v = rows.map(t => n(t.realized_pnl)).filter(x => x !== null);
  const wins = v.filter(x => x > 0), losses = v.filter(x => x < 0);
  const avg = a => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
  const aw = avg(wins), al = avg(losses);
  const gain = wins.reduce((x, y) => x + y, 0), loss = losses.reduce((x, y) => x + y, 0);
  return { wins: wins.length, losses: losses.length, count: v.length, winRate: v.length ? wins.length / v.length : null, profitFactor: loss ? gain / Math.abs(loss) : null,
    best: v.length ? Math.max(...v) : null, worst: v.length ? Math.min(...v) : null, avgWin: aw, avgLoss: al, payoff: aw !== null && al ? aw / Math.abs(al) : null };
}
function leaderRows(trades) {
  return trades.map(t => `<li><a class="row slim" href="#/trade/${encodeURIComponent(t.trade_id)}">
    <span class="sym">${esc(t.symbol)}</span>
    <span class="what">${esc(shortDay(t.day))}, ${esc(sideWord(t.side))}${n(t.qty) === null ? "" : " " + esc(fmt(n(t.qty), 0))}</span>
    <span class="amt ${tone(t.realized_pnl)}">${esc(usd(t.realized_pnl, { sign: true }))}<small>${esc(rmult(t.r))}</small></span></a></li>`).join("");
}
function groupBars(a, dim, order, trim) {
  const groups = Object.values((a.breakdowns || {})[dim] || {});
  if (!groups.length) return `<p class="note">Not enough trades yet.</p>`;
  if (order) groups.sort(order);
  const hidden = trim && groups.length > trim * 2 ? groups.length - trim * 2 : 0;
  if (hidden) groups.splice(trim, hidden);
  const max = Math.max(1, ...groups.map(g => Math.abs(n(g.realized_pnl) || 0)));
  return `<div class="card table-wrap"><table><thead><tr><th>Group</th><th class="num">Trades</th><th class="num">Win rate after costs</th><th class="num">Result</th><th class="hide-sm"><span class="small">Size</span></th></tr></thead><tbody>${groups.map(g => {
    const v = n(g.realized_pnl) || 0;
    return `<tr><td>${esc(String(g.label).replace(/_/g, " "))}${g.trades < 5 ? ` <span class="small muted">(few)</span>` : ""}</td><td class="num">${g.trades}</td><td class="num">${esc(pct(g.win_rate))}</td><td class="num ${tone(v)}">${esc(usd(v))}</td>
      <td class="hide-sm" style="width:26%"><div class="bar ${tone(v)}"><i style="width:${(Math.abs(v) / max) * 100}%"></i></div></td></tr>`;
  }).join("")}</tbody></table></div>${hidden ? `<p class="small muted" style="margin-top:.5rem">${esc(`${hidden} more symbols in between. Filter by symbol on the Trades page.`)}</p>` : ""}`;
}
async function viewOverview() {
  const [ov, an] = await Promise.all([overviewData(), api("/api/analytics")]);
  const everyTrade = { rows: cache.trades };
  const ts = tradeStats(everyTrade.rows);
  const best = { rows: everyTrade.rows.slice(0, 5) };
  const worst = { rows: everyTrade.rows.slice(-5).reverse() };
  const a = ov.periods.all_time;
  if (!a || !a.trades) return band(`<h1 class="display">No trades yet</h1><p class="lede">The journal fills in after the bot's first trading day.</p>`, "hero");
  const eq = ov.equity || {}, pts = eq.points || [];
  const real = n(a.realized_pnl), net = n(a.modeled_net_pnl);
  const ds = dayStats(pts);
  const headline = real > 0 ? `Up <span class="sign-gain">${esc(usd(real))}</span>` : real < 0 ? `Down <span class="sign-loss">${esc(usd(-real))}</span>` : "Flat";
  const lede = `<strong>${esc(plural(a.trades, "trade"))} over ${esc(plural(a.trading_days, "session"))}</strong>: ${esc(`${ts.wins} won, ${ts.losses} lost on fill prices. After modeled trading costs the paper account is ${usd(net, { sign: true })}.`)}${ds ? ` ${esc(`Best session ${usd(ds.best.v, { sign: true })} (${shortDay(ds.best.day)}, all its trades added up), worst session ${usd(ds.worst.v)} (${shortDay(ds.worst.day)}). Best single trade ${usd(ts.best, { sign: true })}, worst ${usd(ts.worst)}.`)}` : ""}${a.sample_note ? " " + esc(cap(a.sample_note) + ".") : ""}`;
  const facts = factList([
    ["Average win", esc(usd(ts.avgWin)), `best single trade ${usd(ts.best)}`],
    ["Average loss", esc(usd(ts.avgLoss)), `worst single trade ${usd(ts.worst)}`],
    ["Win to loss size", esc(ts.payoff === null ? NA : fmt(ts.payoff, 2) + "×"), "average win ÷ average loss"],
    ["Median trade", esc(rmult(a.median_r)), "in units of planned risk"],
    ["Worst 5% of trades", esc(usd(a.cvar_5pct)), "average loss in the tail, after costs"],
    ["Bot errors", esc(`${a.execution_incidents} of ${a.trades}`), `${pct(a.execution_incident_rate)} of trades hit one`],
  ]);
  const days = ds ? `<p class="small muted" style="margin-top:.6rem">${esc(`${ds.green} green and ${ds.red} red sessions. ${ds.run > 1 ? `Current run: ${ds.run} ${ds.runSign > 0 ? "green" : ds.runSign < 0 ? "red" : "flat"} sessions in a row.` : ""}`)}</p>` : "";
  const openRows = (ov.positions && ov.positions.open_trades) || [];
  const findings = (ov.findings && ov.findings.findings) || [];
  const sev = { critical: "none", high: "proposed" };
  const invest = ov.investigation || [];
  const costNote = a.cost_per_trade ? `Modeled costs are ${usd(a.modeled_cost)} in total (${usd(a.cost_per_trade)} per trade). The broker charges no commission on paper, so costs are an estimate.` : "";
  return `
  ${band(`
    <p class="kicker-date">Paper account · everything recorded${pts.length ? ", since " + esc(shortDay(pts[0].day)) : ""}</p>
    <h1 class="display">${headline}</h1>
    <p class="lede">${lede}</p>`, "hero")}
  ${body(`<div class="grid">
    <div class="span-8 stack">
      <section aria-labelledby="o-run">${secHead("Running total", `<span class="legend"><i class="k1"></i>From fill prices <i class="k2"></i>After modeled costs</span>`, "o-run")}
        <div class="card pad">${chartSlot(pts, "cum_realized", { key2: "cum_net" })}</div></section>
      <section aria-labelledby="o-day">${secHead("Each session", "Result per trading day, from fill prices.", "o-day")}
        <div class="card pad">${chartSlot(pts, "daily_realized", { bars: true })}${days}</div></section>
    </div>
    <div class="span-4 stack">
      <section aria-labelledby="o-key">${secHead("Key numbers", "", "o-key")}${facts}
        ${costNote ? `<p class="small muted" style="margin-top:.6rem">${esc(costNote)}</p>` : ""}</section>
      ${openRows.length ? `<section aria-labelledby="o-open">${secHead("Open now", "", "o-open")}<ul class="rows card">${openRows.map(t => tradeRow(t, judgement(t, null))).join("")}</ul></section>` : ""}
    </div>
    <section class="span-6" aria-labelledby="o-best">${secHead("Best trades", `<a href="${href("trades", { sort: "realized_pnl", order: "desc" })}">All trades</a>`, "o-best")}
      <ul class="rows card">${leaderRows(best.rows) || ""}</ul></section>
    <section class="span-6" aria-labelledby="o-worst">${secHead("Worst trades", `<a href="${href("trades", { sort: "realized_pnl", order: "asc" })}">All trades</a>`, "o-worst")}
      <ul class="rows card">${leaderRows(worst.rows) || ""}</ul></section>
    <section class="span-6" aria-labelledby="o-sym">${secHead("By symbol", "Five worst and five best.", "o-sym")}${groupBars(an, "by_symbol", (x, y) => (n(x.realized_pnl) || 0) - (n(y.realized_pnl) || 0), 5)}</section>
    <div class="span-6 stack">
      <section aria-labelledby="o-wd">${secHead("By weekday", "", "o-wd")}${groupBars(an, "by_weekday")}</section>
      <section aria-labelledby="o-time">${secHead("By entry time", "", "o-time")}${groupBars(an, "by_time_of_day")}</section>
    </div>
    ${(findings.length || invest.length) ? `<section class="span-12" aria-labelledby="o-find">${secHead("What the journal flags", `<a href="#/bot">The bot page</a>`, "o-find")}
      <ul class="entries card">${findings.map(f => `<li class="entry"><div class="entry-head"><h3>${esc(f.title)}</h3></div><p>${esc(f.detail)}</p></li>`).join("")}
      ${invest.map(i => `<li class="entry"><div class="entry-head"><h3>${esc(i.title)}</h3><span class="fix ${sev[i.severity] || "not_needed"}">${esc(cap(i.severity))}</span></div><p class="meta">${esc(i.detail || "")}</p></li>`).join("")}</ul></section>` : ""}
  </div>`)}`;
}

/* ------------------------------------------------------------------ Today */
async function viewToday(params) {
  const [h, days, book, overview] = await Promise.all([health(), api("/api/days"), api("/api/mistakes"), api("/api/overview")]);
  const list = (days.days || []).slice().sort();
  const session = h.status.session;
  const etToday = new Date().toLocaleDateString("en-CA", { timeZone: TZ });
  const traded = ((overview.equity && overview.equity.points) || []).map(p => p.day).sort();
  const day = params.day || (traded.includes(session) ? session : traded[traded.length - 1]) || session || list[list.length - 1];
  if (!day) return band(`<h1 class="display">No sessions yet</h1><p class="lede">The journal fills in after the bot's first trading day.</p>`, "hero");
  const isToday = day === session || day === etToday;
  const [closed, open] = await Promise.all([
    api("/api/trades", { from: day, to: day, sort: "entry_time", order: "asc" }),
    api("/api/trades", { only_open: "1" }),
  ]);
  const reviews = Object.fromEntries(book.reviewed.filter(e => e.trade_id).map(e => [e.trade_id, e]));
  const incidents = book.reviewed.filter(e => !e.trade_id && e.day === day);
  const trades = closed.rows;
  const opens = open.rows.filter(t => t.day === day || isToday);
  const agg = closed.aggregate;
  const realized = n(agg.realized_pnl);
  const dayTs = tradeStats(trades);

  let headline;
  if (!trades.length) headline = opens.length ? "Nothing closed yet" : "No trades";
  else if (realized > 0) headline = `Up <span class="sign-gain">${esc(usd(realized))}</span>`;
  else if (realized < 0) headline = `Down <span class="sign-loss">${esc(usd(-realized))}</span>`;
  else headline = "Flat";

  const js = trades.map(t => judgement(t, reviews[t.trade_id]));
  const lossKinds = js.filter((j, i) => n(trades[i].realized_pnl) < 0).map(j => j.kind);
  const count = k => lossKinds.filter(x => x === k).length;
  const wins = trades.filter(t => n(t.realized_pnl) > 0).length;
  const parts = [];
  if (trades.length) {
    const bits = [];
    if (wins) bits.push(plural(wins, "win"));
    if (count("unlucky")) bits.push(plural(count("unlucky"), "normal stop-out"));
    if (count("mistake")) bits.push(plural(count("mistake"), "bot mistake"));
    if (count("pending")) bits.push(`${count("pending")} not reviewed yet`);
    parts.push(`<strong>${esc(plural(trades.length, "trade"))} closed</strong>${bits.length ? ": " + esc(bits.join(", ")) : ""}.`);
    parts.push(esc(`After modeled trading costs: ${usd(agg.modeled_net_pnl)}.`));
  }
  if (opens.length) parts.push(esc(`${opens.map(t => t.symbol).join(", ")} ${opens.length === 1 ? "is" : "are"} still open.`));
  if (!trades.length && !opens.length) parts.push(esc(isToday ? "The bot only enters between 9:35 and 10:30 AM ET." : "No positions were opened on this day."));

  const idx = list.indexOf(day);
  const prev = idx > 0 ? list[idx - 1] : null;
  const next = idx >= 0 && idx < list.length - 1 ? list[idx + 1] : null;
  const st = botState(h);

  const ticker = trades.map(t => `<li>${esc(t.symbol)}<span class="${tone(t.realized_pnl)}">${esc(usd(t.realized_pnl, { sign: true }))}</span></li>`)
    .concat(opens.map(t => `<li>${esc(t.symbol)}<span>open</span></li>`));
  const rows = trades.map((t, i) => tradeRow(t, js[i])).concat(opens.map(t => tradeRow(t, judgement(t, null))));

  const pts = (overview.equity && overview.equity.points) || [];
  const all = overview.periods && overview.periods.all_time;
  const allLine = all ? `Since ${shortDay(pts.length ? pts[0].day : day)}: ${usd(all.realized_pnl)} over ${plural(all.trades, "trade")}, ${plural(all.wins, "win")}.` : "";
  const daily = Object.fromEntries(pts.map(p => [p.day, p]));
  const end = idx >= 0 ? idx + 1 : list.length;
  const week = list.slice(Math.max(0, end - 5), end).map(d => {
    const p = daily[d];
    const v = p ? n(p.daily_realized) : null;
    const label = dayDate(d).toLocaleDateString("en-US", { timeZone: "UTC", weekday: "short", day: "numeric" });
    return `<a href="${href("today", { day: d })}" class="${d === day ? "today" : ""}"${d === day ? ' aria-current="date"' : ""}>
      <span class="d">${esc(label)}</span><span class="v ${tone(v)}">${p ? esc(usd(v, { digits: 0 })) : "—"}</span><span class="n">${p ? esc(`${usd(p.daily_net, { digits: 0 })} after costs`) : "no trades"}</span></a>`;
  }).join("");

  const lessons = book.lessons || [];
  return `
  ${band(`
    <div class="band-row">
      <p class="kicker-date">${esc(longDay(day))}${isToday ? " · today" : ""}</p>
      <nav class="daynav" aria-label="Other days">
        ${prev ? `<a href="${href("today", { day: prev })}">← ${esc(shortDay(prev))}</a>` : ""}
        ${isToday ? "" : `<a href="${href("today")}">Today</a>`}
        ${next ? `<a href="${href("today", { day: next })}">${esc(shortDay(next))} →</a>` : ""}
      </nav>
    </div>
    <h1 class="display">${headline}</h1>
    ${parts.length ? `<p class="lede">${parts.join(" ")}</p>` : ""}
    ${isToday ? `<p class="status"><span class="dot ${st.cls}" aria-hidden="true"></span><span>${esc(st.sentence)}</span> <a href="#/bot">See why</a></p>` : ""}`, "hero")}
  ${ticker.length ? `<div class="ticker" aria-label="Results"><ul>${ticker.join("")}</ul></div>` : ""}
  ${body(`<div class="grid">
    <section class="span-7" aria-labelledby="t-trades">
      ${secHead(isToday ? "Today's trades" : "Trades", "Every loss gets a verdict: bad luck, or a bot mistake to fix.", "t-trades")}
      ${rows.length ? `<ul class="rows card">${rows.join("")}</ul>` : `<p class="note">No positions were opened on this day.</p>`}
      ${trades.length ? `<div style="margin-top:1.25rem">${factList([
        ["Win rate", esc(pct(dayTs.winRate)), `${dayTs.wins} won, ${dayTs.losses} lost`],
        ["Best trade", `<span class="${tone(dayTs.best)}">${esc(usd(dayTs.best, { sign: true }))}</span>`],
        ["Worst trade", `<span class="${tone(dayTs.worst)}">${esc(usd(dayTs.worst, { sign: true }))}</span>`],
        ["Modeled costs", esc(usd(agg.modeled_cost)), `${usd(agg.modeled_net_pnl, { sign: true })} after costs`],
        ["Average trade", esc(rmult(agg.mean_r)), "in units of planned risk"],
        ["Bot errors", esc(`${agg.execution_incidents} of ${agg.trades}`), "trades hit an execution problem"],
      ])}</div>` : ""}
    </section>
    <div class="span-5 stack">
      <section aria-labelledby="t-run">
        ${secHead("Running total", esc(allLine), "t-run")}
        <div class="card pad">${chartSlot(pts, "cum_realized")}</div>
        <p class="small muted" style="margin-top:.6rem">Paper money, from the bot's own fill prices. <a href="#/research">Look closer in Research</a></p>
      </section>
      ${week ? `<section aria-labelledby="t-week">${secHead("Recent sessions", "", "t-week")}<div class="card week">${week}</div></section>` : ""}
      ${incidents.length ? `<section class="card ink" aria-labelledby="t-inc"><div class="card-head">${secHead("Problems that cost nothing", "", "t-inc")}</div>
        <ul class="entries">${incidents.map(e => `<li class="entry"><div class="entry-head"><h3>${esc(e.title)}</h3>${fixTag(e.fix)}</div><p class="meta">${esc(e.cause)}</p></li>`).join("")}</ul></section>` : ""}
      ${lessons.length ? `<section class="card ink" aria-labelledby="t-learn"><div class="card-head">${secHead("What the bot learned", `<a href="#/mistakes">All mistakes</a>`, "t-learn")}</div>
        ${lessonRows(lessons.slice(0, 3))}</section>` : ""}
    </div>
  </div>`)}`;
}
function tradeRow(t, j) {
  const isOpen = j.kind === "open";
  const times = isOpen ? `since ${etTime(t.entry_time)}` : `${etTime(t.entry_time)} to ${etTime(t.exit_time)}`;
  const qty = n(isOpen ? t.open_qty : t.qty);
  const amt = isOpen ? `<span class="amt muted">open</span>`
    : `<span class="amt ${tone(t.realized_pnl)}">${esc(usd(t.realized_pnl, { sign: true }))}<small>${esc(rmult(t.r))}</small></span>`;
  return `<li><a class="row" href="#/trade/${encodeURIComponent(t.trade_id)}">
    <span class="sym">${esc(t.symbol)}</span>
    <span class="what"><b>${esc(sideWord(t.side))}${qty === null ? "" : " " + esc(fmt(qty, 0)) + " shares"}</b>, ${esc(times)}</span>
    ${amt}
    ${verdict(j)}
  </a></li>`;
}

/* ------------------------------------------------------------------ Trades */
const SORTS = { day: "entry_time", symbol: "symbol", pnl: "realized_pnl", r: "r", held: "holding_seconds", cost: "modeled_cost" };
let lastTradeRows = [];
async function viewTrades(params) {
  const f = { symbol: params.symbol || "", side: params.side || "", loss_class: params.loss_class || "", from: params.from || "", to: params.to || "" };
  const sortKey = SORTS[params.sort] ? params.sort : (Object.keys(SORTS).find(k => SORTS[k] === params.sort) || "day");
  const order = params.order === "asc" ? "asc" : "desc";
  const [d, book] = await Promise.all([api("/api/trades", { ...f, sort: SORTS[sortKey], order }), api("/api/mistakes")]);
  const reviews = Object.fromEntries(book.reviewed.filter(e => e.trade_id).map(e => [e.trade_id, e]));
  lastTradeRows = d.rows;
  const a = d.aggregate;
  const tw = tradeStats(d.rows);
  const opts = d.options || {};
  const sel = (name, label, values, words) => `<label class="field">${label}<select name="${name}"><option value="">All</option>${(values || []).map(v => `<option value="${esc(v)}"${f[name] === v ? " selected" : ""}>${esc(words ? words[v] || v : v)}</option>`).join("")}</select></label>`;
  const filtered = Object.values(f).some(Boolean);
  const th = (key, label, cls) => {
    const on = key === sortKey, next = on && order === "desc" ? "asc" : "desc";
    return `<th class="${cls || ""}"${on ? ` aria-sort="${order === "asc" ? "ascending" : "descending"}"` : ""}><a class="sort" href="${href("trades", { ...f, sort: key, order: next })}">${esc(label)}<span aria-hidden="true">${on ? (order === "asc" ? " ↑" : " ↓") : ""}</span></a></th>`;
  };
  const rows = d.rows.map(t => {
    const j = judgement(t, reviews[t.trade_id]);
    return `<tr class="link" data-href="#/trade/${encodeURIComponent(t.trade_id)}">
      <td class="nowrap">${esc(shortDay(t.day))}<span class="small muted hide-sm"> ${esc(etTime(t.entry_time))}</span></td>
      <td><a class="sym" href="#/trade/${encodeURIComponent(t.trade_id)}">${esc(t.symbol)}</a></td>
      <td class="hide-sm">${esc(sideWord(t.side))}${n(t.qty) === null ? "" : " " + esc(fmt(n(t.qty), 0))}</td>
      <td class="num ${tone(t.realized_pnl)}">${esc(usd(t.realized_pnl, { sign: true }))}</td>
      <td class="num hide-sm">${esc(rmult(t.r))}</td>
      <td class="num hide-sm">${esc(usd(t.modeled_cost))}</td>
      <td class="num hide-sm">${esc(held(t.holding_seconds))}</td>
      <td class="hide-sm">${esc((t.exit_reason || NA).replace(/_/g, " "))}</td>
      <td>${verdict(j)}</td></tr>`;
  }).join("");
  const foot = `<tfoot><tr><td colspan="2">${esc(plural(a.trades, "trade"))}</td><td class="hide-sm">${tw.wins}W ${tw.losses}L</td><td class="num ${tone(a.realized_pnl)}">${esc(usd(a.realized_pnl, { sign: true }))}</td>
    <td class="num hide-sm">${esc(rmult(a.expectancy_r))}</td><td class="num hide-sm">${esc(usd(a.modeled_cost))}</td><td class="hide-sm"></td><td class="hide-sm"></td><td>${esc(usd(a.modeled_net_pnl, { sign: true }))} after costs</td></tr></tfoot>`;
  return `
  ${band(`
    <p class="kicker-date">${opts.day_from ? esc(`${shortDay(opts.day_from)} to ${shortDay(opts.day_to)}`) : "Every closed trade"}</p>
    <h1 class="display">Trades</h1>
    <p class="lede"><strong>${esc(plural(a.trades, "closed trade"))}${filtered ? " match" : ""}</strong>: ${esc(`${usd(a.realized_pnl, { sign: true })} in total, ${usd(a.modeled_net_pnl, { sign: true })} after modeled costs. ${plural(tw.wins, "win")}, ${plural(tw.losses, "loss", "losses")}, win rate ${pct(tw.winRate)}.`)}</p>
    <form class="filters" id="trade-filters">
      <input type="hidden" name="sort" value="${esc(sortKey)}"><input type="hidden" name="order" value="${order}">
      <label class="field">Symbol<input name="symbol" value="${esc(f.symbol)}" placeholder="e.g. EWZ" autocomplete="off" spellcheck="false"></label>
      ${sel("side", "Side", opts.side)}
      ${sel("loss_class", "What caused it", opts.loss_class, LOSS_WORDS)}
      <label class="field">From<input type="date" name="from" value="${esc(f.from)}"></label>
      <label class="field">To<input type="date" name="to" value="${esc(f.to)}"></label>
      <button class="btn" type="submit">Show trades</button>
      ${filtered ? `<a href="#/trades" class="btn ghost">Clear filters</a>` : ""}
      <button class="btn ghost" type="button" id="trade-csv"${d.rows.length ? "" : " disabled"}>Download CSV</button>
    </form>`)}
  ${body(`
    ${d.rows.length ? `<div class="card table-wrap"><table>
      <thead><tr>${th("day", "Day")}${th("symbol", "Symbol")}<th class="hide-sm">Side and size</th>${th("pnl", "Result", "num")}${th("r", "In R", "num hide-sm")}${th("cost", "Cost", "num hide-sm")}${th("held", "Held", "num hide-sm")}<th class="hide-sm">How it ended</th><th>Verdict</th></tr></thead>
      <tbody>${rows}</tbody>${foot}</table></div>` : `<p class="note">No closed trades match these filters. <a href="#/trades">Show all trades</a></p>`}
    <p class="small muted" style="margin-top:.75rem">R is the result divided by the risk planned at entry. Cost is modeled, not charged by the broker. Click a column to sort.</p>`)}`;
}
const CSV_COLS = ["trade_id", "day", "symbol", "side", "qty", "entry_time", "exit_time", "entry_vwap", "exit_vwap", "planned_stop", "realized_pnl", "modeled_cost", "modeled_net_pnl", "r", "holding_seconds", "exit_reason", "loss_class"];
function downloadCsv(rows) {
  const cell = v => { const t = v === null || v === undefined ? "" : String(v); return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
  const text = [CSV_COLS.join(",")].concat(rows.map(r => CSV_COLS.map(c => cell(r[c])).join(","))).join("\n");
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url; a.download = "paper-trades.csv";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function wireTrades(root) {
  const form = $("#trade-filters", root);
  if (form) form.addEventListener("submit", ev => {
    ev.preventDefault();
    go(href("trades", Object.fromEntries(new FormData(form))));
  });
  const csv = $("#trade-csv", root);
  if (csv) csv.addEventListener("click", () => downloadCsv(lastTradeRows));
}

/* ------------------------------------------------------------------ one trade */
async function viewTrade(id) {
  const [d, book] = await Promise.all([api("/api/trades/" + encodeURIComponent(id)), api("/api/mistakes")]);
  const t = d.trade;
  const review = book.reviewed.find(e => e.trade_id === t.trade_id);
  const j = judgement(t, review);
  const isOpen = j.kind === "open";
  const field = (label, value) => `<div><dt>${esc(label)}</dt><dd>${value}</dd></div>`;
  const price = v => (n(v) === null ? NA : esc("$" + fmt(n(v), 2)));
  const steps = (d.steps || []).map(s => `<li><span class="state ${["missing", "failed", "rejected"].includes(s.state) ? "neg" : ""}">${esc(s.state || "")}</span><span>${esc(s.label)}</span></li>`).join("");
  const fills = (d.fills || []).map(x => `<tr><td class="nowrap">${esc(etTime(x.ts))}</td><td>${esc(x.role)}</td><td>${esc(x.side)}</td><td class="num">${esc(fmt(n(x.qty) || 0, 0))}</td><td class="num">${price(x.price)}</td></tr>`).join("");
  const why = review
    ? `<div class="entry"><div class="entry-head"><h3>${esc(review.title)}</h3>${review.verdict === "mistake" ? fixTag(review.fix) : ""}</div><p>${esc(review.cause)}</p>${review.evidence ? `<details><summary>Evidence</summary><p class="evidence">${esc(review.evidence)}</p></details>` : ""}</div>`
    : `<div class="entry"><p>${isOpen ? "This position is still open." : esc(`The journal's first read: ${LOSS_WORDS[t.loss_class] || t.loss_class}.`)}</p>${(t.loss_reasons || []).length ? `<p class="meta">${esc(t.loss_reasons.join("; "))}</p>` : ""}${!isOpen && n(t.realized_pnl) < 0 ? `<p class="meta">The daily review hasn't judged this loss yet.</p>` : ""}</div>`;
  const pnlCls = isOpen ? "" : n(t.realized_pnl) < 0 ? "sign-loss" : n(t.realized_pnl) > 0 ? "sign-gain" : "";
  return `
  ${band(`
    <div class="band-row">
      <p class="kicker-date">${esc(sideWord(t.side))} · ${esc(longDay(t.day))}</p>
      <nav class="daynav" aria-label="Back"><a href="#/trades" data-back>← Back</a><a href="${href("today", { day: t.day })}">That day</a>${stockChart(t.symbol, "5-minute chart")}</nav>
    </div>
    <div class="band-row">
      <h1 class="display">${esc(t.symbol)}</h1>
      <p class="display big ${pnlCls}">${isOpen ? "Open" : esc(usd(t.realized_pnl, { sign: true }))}</p>
    </div>
    <p class="lede">${isOpen ? esc(`Stop at ${t.final_stop ?? NA}.`) : `<strong>${esc(rmult(t.r))}</strong> ${esc(`of the risk planned at entry, held ${held(t.holding_seconds)}.`)}`}</p>`)}
  ${body(`<div class="grid">
    <section class="span-7 stack">
      <div class="card"><div class="card-head">${verdict(j)}</div>
      <dl class="fields">
        ${field("Shares", esc(fmt(n(t.qty) || 0, 0)))}
        ${field("Entry", `${price(t.entry_vwap)} <small>plan ${price(t.planned_entry)}</small>`)}
        ${field("Exit", price(t.exit_vwap))}
        ${field("Stop", `${price(t.final_stop)} <small>plan ${price(t.planned_stop)}</small>`)}
        ${field("Risk at entry", esc(usd(t.initial_risk)))}
        ${field("Held for", esc(held(t.holding_seconds)))}
        ${field("How it ended", esc((t.exit_reason || NA).replace(/_/g, " ")))}
        ${field("Costs (modeled)", esc(usd(t.modeled_cost)))}
      </dl></div>
      <div>${secHead("Fills", "Every share bought or sold, Eastern time.")}
        ${fills ? `<div class="card table-wrap"><table><thead><tr><th>Time (ET)</th><th>Role</th><th>Side</th><th class="num">Shares</th><th class="num">Price</th></tr></thead><tbody>${fills}</tbody></table></div>` : `<p class="note">No fills recorded.</p>`}</div>
    </section>
    <div class="span-5 stack">
      <section class="card ink"><div class="card-head">${secHead("Why it went this way", "")}</div>${why}</section>
      <section>${secHead("Step by step", "Each step the bot should record, and whether it did.")}
        <ul class="timeline card">${steps || `<li><span class="muted">No steps recorded.</span></li>`}</ul></section>
      ${(d.limitations || []).length ? `<p class="small muted">${esc(d.limitations.join(" "))}</p>` : ""}
    </div>
  </div>`)}`;
}

/* ------------------------------------------------------------------ Calendar */
async function viewCalendar(params) {
  const d = await api("/api/calendar", { month: params.month || "" });
  const today = new Date().toLocaleDateString("en-CA", { timeZone: TZ });
  const cal = d.calendar;
  const t = cal.totals || {};
  const first = dayDate(cal.month + "-01").getUTCDay();
  const blanks = Array.from({ length: first }, () => `<div class="cell blank" aria-hidden="true"></div>`).join("");
  const cells = cal.cells.map(c => {
    const num = Number(c.day.slice(8));
    const now = c.day === today ? " is-today" : "";
    if (c.state === "market_closed" || c.day > today) return `<div class="cell quiet${now}"><span class="d">${num}</span></div>`;
    if (c.state !== "traded") {
      const label = { healthy_no_trades: "No trades", not_run: "Missed", unavailable: "Not run" }[c.state] || "";
      return `<div class="cell quiet${now}" title="${esc(c.detail || "")}"><span class="d">${num}</span><span class="n">${esc(label)}</span></div>`;
    }
    const v = n(c.realized_pnl);
    return `<a class="cell cal-cell ${v > 0 ? "up" : v < 0 ? "down" : ""}${now}" href="${href("today", { day: c.day })}" aria-label="${esc(longDay(c.day))}: ${esc(usd(v))}, ${esc(plural(c.trades, "trade"))}">
      <span class="d">${num}</span><span class="v ${tone(v)}">${esc(usd(v, { digits: 0 }))}</span><span class="n">${esc(plural(c.trades, "trade"))}</span></a>`;
  }).join("");
  const months = (d.months || []).slice().reverse();
  return `
  ${band(`
    <p class="kicker-date">Calendar</p>
    <h1 class="display">${esc(monthName(cal.month))}</h1>
    <p class="lede"><strong>${esc(plural(t.traded || 0, "day"))} with trades</strong>, ${esc(usd(t.realized_pnl))} in total.${t.not_run ? ` ${esc(`The bot missed the opening scan on ${plural(t.not_run, "day")}.`)}` : ""}</p>
    <nav class="tabs" aria-label="Months">${months.map(m => `<a href="${href("calendar", { month: m })}" aria-current="${m === cal.month}">${esc(monthName(m))}</a>`).join("")}</nav>`)}
  ${body(`
    <div class="cal" role="grid" aria-label="${esc(monthName(cal.month))}">
      ${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map(w => `<div class="wd" aria-hidden="true">${w}</div>`).join("")}
      ${blanks}${cells}
    </div>
    <p class="small muted" style="margin-top:1rem">Pick a trading day to read it. Dashed days had no trades: the market was closed, the rules found nothing, or the bot didn't run.</p>`)}`;
}

/* ------------------------------------------------------------------ Mistakes */
function fixTag(fix) {
  const s = (fix && fix.status) || "none";
  const words = { none: "Not fixed yet", proposed: "Fix waiting for your OK", approved: "Fix approved", shipped: "Fixed", not_needed: "No fix needed" };
  return `<span class="fix ${esc(s)}">${esc(words[s] || s)}</span>`;
}
async function viewMistakes() {
  const d = await api("/api/mistakes");
  const s = d.summary;
  const mistakes = d.reviewed.filter(e => e.verdict === "mistake");
  const current = mistakes.filter(e => e.era !== "pre-hardening");
  const old = mistakes.filter(e => e.era === "pre-hardening");
  const lucky = d.reviewed.filter(e => e.verdict === "unlucky");
  const unknown = d.reviewed.filter(e => e.verdict === "unknown");
  const entry = e => `<li class="entry">
      <div class="entry-head"><h3>${esc(e.title)}</h3>${e.verdict === "mistake" ? fixTag(e.fix) : ""}</div>
      <p class="meta">${esc(shortDay(e.day))}${e.symbol ? ", " + esc(e.symbol) : ""}${n(e.realized_pnl) !== null ? ", " + esc(usd(e.realized_pnl, { sign: true })) : ", no money lost"}${e.category ? ", " + esc(catWords(e.category)) : ""}${e.trade_id ? ` · <a href="#/trade/${encodeURIComponent(e.trade_id)}">Open trade</a>` : ""}</p>
      <p>${esc(e.cause)}</p>
      ${e.fix && (e.fix.test || e.fix.branch || e.fix.commit) ? `<p class="meta">${e.fix.commit ? "Commit " + esc(e.fix.commit) + ". " : e.fix.branch ? "Branch " + esc(e.fix.branch) + ". " : ""}${e.fix.test ? "Test: " + esc(e.fix.test) : ""}</p>` : ""}
      ${e.evidence ? `<details><summary>Evidence</summary><p class="evidence">${esc(e.evidence)}</p></details>` : ""}
    </li>`;
  const waitRow = r => `<tr class="link" data-href="#/trade/${encodeURIComponent(r.trade_id)}">
      <td class="nowrap">${esc(shortDay(r.day))}</td><td><a class="sym" href="#/trade/${encodeURIComponent(r.trade_id)}">${esc(r.symbol)}</a></td>
      <td class="num neg">${esc(usd(r.realized_pnl))}</td><td>${verdict({ kind: "pending", word: r.suggested === "mistake" ? "Mistake?" : "Bad luck?", why: "", title: (r.loss_reasons || []).join("; ") })}</td></tr>`;
  const waitTable = rows => `<div class="card table-wrap"><table><thead><tr><th>Day</th><th>Symbol</th><th class="num">Loss</th><th>First guess</th></tr></thead><tbody>${rows.map(waitRow).join("")}</tbody></table></div>`;
  const firstRows = d.awaiting_review.slice(0, 8), moreRows = d.awaiting_review.slice(8);
  const waiting = firstRows.length ? waitTable(firstRows) + (moreRows.length ? `<details style="margin-top:.75rem"><summary>Show ${moreRows.length} more</summary>${waitTable(moreRows)}</details>` : "") : "";
  const lessons = d.lessons || [];
  let lede;
  if (!d.reviewed.length) lede = esc("No losses have been reviewed yet. The daily review runs after each close.");
  else lede = `<strong>${esc(plural(s.mistakes, "bot mistake"))} found, ${esc(s.fixed)} fixed.</strong> ${esc(`${s.pre_hardening ? `${s.pre_hardening} happened before the Oct 2 safety hardening; ${s.current} since. ` : ""}${s.fix_waiting_approval ? `${plural(s.fix_waiting_approval, "fix")} ${s.fix_waiting_approval === 1 ? "is" : "are"} waiting for your OK. ` : ""}${plural(s.unlucky, "loss", "losses")} ${s.unlucky === 1 ? "was" : "were"} bad luck${s.unknown ? `; ${s.unknown} cannot be judged from the record` : ""}.`)}`;
  return `
  ${band(`
    <p class="kicker-date">The loss journal</p>
    <h1 class="display">Mistakes</h1>
    <p class="lede">${lede}</p>`)}
  ${body(`
    ${d.errors.length ? `<p class="note bad" style="margin-bottom:1.25rem">${esc(plural(d.errors.length, "line"))} in the loss journal could not be read: ${esc(d.errors.map(x => `line ${x.line}: ${x.error}`).join("; "))}</p>` : ""}
    ${d.state === "missing" ? `<p class="note" style="margin-bottom:1.25rem">The loss journal file doesn't exist yet (${esc(d.path)}). The first daily review creates it.</p>` : ""}
    <div class="grid">
      <div class="span-7 stack">
        <section>${secHead("Bot mistakes since the hardening", "Each one gets a fix and a test, so it can't happen the same way again.")}
          ${current.length ? `<ul class="entries card">${current.map(entry).join("")}</ul>` : `<p class="note">No bot mistakes recorded.</p>`}</section>
        ${old.length ? `<section>${secHead("Before the Oct 2 hardening", "Old failures the later safety work was built to stop. Kept so a repeat is recognized.")}
          <details class="card pad"><summary>Show ${plural(old.length, "old mistake")}</summary><ul class="entries">${old.map(entry).join("")}</ul></details></section>` : ""}
        <section>${secHead("Bad luck", "The bot did what it should; the market went the other way.")}
          ${lucky.length ? `<details class="card pad"><summary>Show ${plural(lucky.length, "bad-luck loss", "bad-luck losses")}</summary><ul class="entries">${lucky.map(entry).join("")}</ul></details>` : `<p class="note">None recorded.</p>`}</section>
        ${unknown.length ? `<section>${secHead("Cannot be judged", "The record does not support either verdict. Marked unknown rather than guessed.")}<ul class="entries card">${unknown.map(entry).join("")}</ul></section>` : ""}
      </div>
      <div class="span-5 stack">
        <section class="card ink"><div class="card-head">${secHead("What the bot learned", esc(`${plural(lessons.length, "kind")} of mistake`))}
          <p class="small muted" style="margin:-.4rem 0 .5rem">The daily review reads this before judging new losses. A fixed kind that comes back is flagged.</p></div>
          ${lessons.length ? lessonRows(lessons) : `<p class="entry meta">Nothing yet.</p>`}</section>
        <section>${secHead("Not reviewed yet", esc(`${plural(s.awaiting_review, "older loss", "older losses")}; the verdict is the journal's first guess.`))}
          ${waiting || `<p class="note">Every loss has been reviewed.</p>`}</section>
      </div>
    </div>`)}`;
}

/* ------------------------------------------------------------------ Research */
const DIMENSIONS = [
  ["by_side", "Long or short"], ["by_time_of_day", "Time of entry"], ["by_exit_reason", "How it ended"],
  ["by_loss_class", "Cause"], ["by_relvol", "Relative volume"], ["by_weekday", "Weekday"], ["by_symbol", "Symbol"],
];
async function viewResearch(params) {
  const tab = params.tab || "summary";
  const tabs = [["summary", "Summary"], ["paired", "A versus B"], ["gapup", "Gap-up 10:00"], ["breakdown", "Breakdowns"], ["funnel", "Why it skipped trades"], ["backtest", "Paper vs backtest"], ["pipeline", "Improvement pipeline"]];
  let r;
  if (tab === "breakdown") r = await researchBreakdown(params);
  else if (tab === "funnel") r = await researchFunnel(params);
  else if (tab === "backtest") r = await researchBacktest();
  else if (tab === "pipeline") r = await researchPipeline();
  else if (tab === "paired") r = await researchPaired();
  else if (tab === "gapup") r = await researchGapup();
  else r = await researchSummary();
  return `
  ${band(`
    <p class="kicker-date">Patterns across every trade</p>
    <h1 class="display">Research</h1>
    <p class="lede">${r.lede || esc("Small samples are labeled; don't read much into them yet.")}</p>
    <nav class="tabs" aria-label="Research sections">${tabs.map(([k, l]) => `<a href="${href("research", { tab: k })}" aria-current="${k === tab}">${l}</a>`).join("")}</nav>
    ${r.controls || ""}`)}
  ${body(r.body)}`;
}
function factList(items) {
  return `<dl class="facts card">${items.map(([k, v, note]) => `<div class="fact"><dt>${esc(k)}</dt><dd>${v}${note ? `<small>${esc(note)}</small>` : ""}</dd></div>`).join("")}</dl>`;
}
async function researchPaired() {
  const d = await api("/api/operations");
  const forward = d.forward || {}, qualification = forward.qualification || {};
  const portfolios = Object.entries(forward.portfolios || {});
  const comparison = portfolios.map(([name, lane]) => `<tr><td>${name === "baseline_a" ? "Baseline A" : "Candidate B"}</td><td>${lane.closed_trips.length}</td><td class="num">${esc(usd(lane.totals?.gross_pnl))}</td><td class="num">${esc(usd(lane.totals?.modeled_cost))}</td><td class="num">${esc(usd(lane.totals?.net_modeled_pnl))}</td><td class="num">${esc(usd(lane.drawdown))}</td></tr>`).join("");
  const details = portfolios.map(([name, lane]) => {
    const trips = (lane.closed_trips || []).map(t => `<li><details><summary class="shadow-toggle">${esc(t.day)} · ${esc(t.symbol)} · ${esc(t.qty)} shares · ${esc(usd(t.net_modeled_pnl))} after costs</summary><p>${stockChart(t.symbol, "5-minute chart")}</p><p>${esc(t.classification.replace(/_/g, " "))}. Fill cash ${esc(usd(t.gross_pnl))}; modeled costs ${esc(usd(t.modeled_cost))}; doubled costs ${esc(usd(t.modeled_net_2x))}.</p><div class="table-wrap"><table><thead><tr><th>Time</th><th>Role</th><th>Side</th><th>Shares</th><th>Price</th><th>Modeled costs</th></tr></thead><tbody>${(t.fills || []).map(f => `<tr><td title="${esc(f.ts)}">${esc(etTime(f.ts))} ET</td><td>${esc(f.role)}</td><td>${esc(f.side)}</td><td>${esc(f.qty)}</td><td>${esc(usd(f.price))}</td><td>${esc(usd(Number(f.commission) + Number(f.slippage)))}</td></tr>`).join("")}</tbody></table></div></details></li>`).join("");
    const rejected = (lane.decisions?.all_signal_decisions || []).filter(s => s.reason).map(s => `<tr><td>${stockChart(s.symbol)}</td><td>${esc(s.status)}</td><td>${esc(s.reason)}</td></tr>`).join("");
    const pending = (lane.orders || []).filter(o => !["filled", "canceled", "expired", "replaced", "rejected"].includes(o.status)).map(o => `<li>${esc(o.symbol)} ${esc(o.role)}: ${esc(o.filled_qty)} / ${esc(o.qty)} filled (${esc(o.status)})</li>`).join("");
    return `<section>${secHead(name === "baseline_a" ? "A hypothetical trades" : "B hypothetical trades", "Separate portfolio; fill prices and quantities are models from observed IEX quotes.")}<ul class="entries card">${trips || "<li>No closed hypothetical trades yet.</li>"}${pending}</ul><p class="small">Quote evidence: ${esc(Object.entries(lane.quote_health || {}).map(([s, reason]) => `${s}: ${reason}`).join("; ") || "No quotes consumed yet")}</p><details><summary class="shadow-toggle">Rejected entries and filters</summary><div class="card table-wrap"><table><thead><tr><th>Symbol</th><th>Status</th><th>Reason</th></tr></thead><tbody>${rejected || '<tr><td colspan="3">No decisions recorded yet.</td></tr>'}</tbody></table></div></details></section>`;
  }).join("");
  const p = d.paired || {}, plans = p.paired_plans || {}, quotes = p.quote_decisions || {};
  const rows = Object.entries(plans).map(([symbol, plan]) => {
    const q = quotes[symbol] || {}, ab = q.ab || {};
    return `<tr><td>${stockChart(symbol)}</td><td>${esc(plan.baseline_qty ?? NA)}</td><td>${esc(plan.candidate_qty ?? NA)}</td><td>${esc(ab.baseline_reason || plan.baseline_reason || (q.ab ? "eligible at submission" : "quote decision unavailable"))}</td><td>${esc(ab.candidate_reason || plan.candidate_reason || (q.ab ? "eligible at submission" : "quote decision unavailable"))}</td><td>${esc(q.quote_age_seconds ?? NA)}</td><td>${esc(ab.decision_time || plan.decision_time || NA)}</td></tr>`;
  }).join("");
  return {lede: "Forward observations from the same decision. Candidate B remains unqualified; simulated fills are hypothetical.",
    body: `${factList([["Collector", esc(d.runtime_mode), "PAPER entries remain paused"], ["Observation session", esc(d.paired_day), p.status], ["Performance comparison", esc(d.performance_comparison)], ["Filtered winners and losers", esc(d.excluded_winners)], ["Execution cost reduction", esc(d.cost_reduction)]])}
    <section>${secHead("TradingView stock charts", "Independent chart projections. Basic plan checked October 28, 2026.")}
      <div class="card pad stack">
        <p>${stockChart("NASDAQ:AAPL", "AAPL 5-minute chart")} · ${stockChart("NASDAQ:MSFT", "MSFT 5-minute chart")} · <a class="chart-link" href="#/trades">Recorded Alpaca PAPER trades</a> · <a class="chart-link" href="${href("research", {tab: "paired"})}">Refresh SHADOW evidence</a></p>
        <p>Use regular-session candles and select A or B in Strategy Tester. The saved baseline and existing historical results are preserved. Historical sources <code>research/pine/baseline_a.pine</code> and <code>candidate_b.pine</code> stop entries at October 29, 2026, 00:00 UTC. For subsequent completed bars, use the separate <code>baseline_a_forward_v1.pine</code> or <code>candidate_b_forward_v1.pine</code>: forward start October 29 UTC; optional end date is disabled by default. Basic permits one saved script; forward copies can run unsaved without overwriting the saved baseline. Settings display dates in the chart timezone; timestamps and the opening session use UTC and New York respectively.</p>
        <p>TradingView uses its own feed and bar-based simulated fills. Chart trades and P&amp;L are not imported or synchronized with this journal. Pine lacks the portfolio universe, IEX quote replay, borrow and cancel-race evidence. B blocks all chart entries because historical quote/depth evidence is unavailable; zero trades is not a performance result.</p>
        <p>Alpaca SHADOW automatically records A/B decisions, hypothetical fills, costs and P&amp;L below. Refresh to read new observations. Recorded PAPER history remains separate. No broker orders are sent from this journal.</p>
        <p>Basic supports three price alerts, but no strategy alerts or webhook notifications. No alert receiver is active. <a class="chart-link" href="https://www.tradingview.com/pricing/#comparison" target="_blank" rel="noopener noreferrer">TradingView plan limits</a>.</p>
      </div>
    </section>
    <section>${secHead("Paired signals", "IEX quotes and conservative size checks; no consolidated depth or queue guarantee.")}<div class="card table-wrap"><table><thead><tr><th>Symbol</th><th>A shares</th><th>B shares</th><th>A decision</th><th>B decision</th><th>Quote age (s)</th><th>Decision time (UTC)</th></tr></thead><tbody>${rows || '<tr><td colspan="7">No paired forward signals collected yet. Missing history is not backfilled.</td></tr>'}</tbody></table></div></section>
    <section>${secHead("Independent SHADOW portfolios", `${forward.matched_count || 0} matched closed outcomes. ${qualification.complete_sessions || 0}/60 complete sessions; ${qualification.candidate_trades || 0}/200 B trades. Qualification remains blocked.`)}<div class="card table-wrap"><table><thead><tr><th>Portfolio</th><th>Closed</th><th>Fill cash</th><th>Modeled costs</th><th>Modeled net</th><th>Drawdown</th></tr></thead><tbody>${comparison}</tbody></table></div><p class="small">Drawdown uses closed net trades in exit order; open excursions are excluded. The proposed $1,000 absolute equity drawdown limit is inactive and requires separate risk approval.</p></section>${details}`};
}
async function researchGapup() {
  const d = await api("/api/gapup");
  const row = (label, m, status) => `<tr><td>${esc(label)}</td><td>${m.trades}</td><td class="num">${esc(usd(m.net_1x))}</td><td class="num">${esc(usd(m.net_2x))}</td><td class="num">${m.expectancy_1x == null ? NA : esc(usd(m.expectancy_1x))}</td><td class="num">${m.win_rate == null ? NA : esc(pct(m.win_rate))}</td><td class="num">${m.profit_factor == null ? NA : esc(fmt(m.profit_factor, 2))}</td><td class="num">${esc(usd(m.max_drawdown))}</td><td>${m.ci95_mean_per_trade ? esc(usd(m.ci95_mean_per_trade[0]) + " to " + usd(m.ci95_mean_per_trade[1])) : NA}</td><td>${m.best_symbol ? esc(m.best_symbol.symbol + " " + usd(m.best_symbol.net) + " (without: " + usd(m.best_symbol.net_without) + ")") : NA}</td><td>${m.small_sample ? "under 30 trades: not meaningful" : ""}</td></tr>`;
  const gp = Object.values(d.portfolios || {}), orb = Object.values(d.same_period_orb || {});
  const quality = gp.map(p => `<li><strong>${esc(p.label)}</strong>: ${esc(p.quality.days_decided ?? 0)} days decided, ${esc(p.quality.days_inputs_failed ?? 0)} with missing inputs, ${esc(p.quality.submitted ?? 0)} entries submitted. Rejected or filtered: ${esc(Object.entries(p.quality.rejected_or_filtered || {}).slice(0, 8).map(([k, v]) => k + " " + v).join("; ") || "none yet")}. Quote checks: ${esc(Object.entries(p.quality.quote_decisions || {}).map(([k, v]) => k + " " + v).join("; ") || "none yet")}.</li>`).join("");
  const trades = gp.map(p => `<section>${secHead(p.label + " closed trades", "Hypothetical fills from observed IEX quotes.")}<ul class="entries card">${(p.trades || []).map(t => `<li>${esc(t.day)} · ${stockChart(t.symbol)} · ${esc(t.qty)} shares · ${esc(usd(t.net_modeled_pnl))} after costs (${esc(t.classification)})</li>`).join("") || "<li>No closed hypothetical trades yet.</li>"}</ul></section>`).join("");
  const s = d.sessions || {};
  return {lede: "Forward SHADOW of the two 10:00 gap-up strategies next to the ORB lanes. Hypothetical fills; not validation; nothing is promoted.",
    body: `${factList([["Version", esc(d.version || NA), "rules frozen at first run"], ["Forward since", esc(d.since || NA)], ["Sessions", esc(`${s.complete || 0} complete of ${s.recorded || 0} recorded`), "incomplete sessions are excluded from counts"], ["October backtest", "history only", "+$4,116 Top-10 10:00 entry was hypothetical and tuned on the same period"]])}
    <section>${secHead("Same-period comparison", "ORB lanes are limited to days since the gap-up collector started. Net columns: 1x and 2x modeled costs.")}<div class="card table-wrap"><table><thead><tr><th>Portfolio</th><th>Trades</th><th>Net 1x</th><th>Net 2x</th><th>Expectancy</th><th>Win rate</th><th>Profit factor</th><th>Drawdown</th><th>95% interval per trade</th><th>Concentration</th><th>Sample</th></tr></thead><tbody>${gp.map(p => row(p.label, p.metrics)).join("")}${orb.map(p => row(p.label, p.metrics)).join("")}</tbody></table></div></section>
    <section>${secHead("Data quality and rejected trades", "Missing quotes and rejections are recorded, never filled in.")}<ul class="entries card">${quality}</ul>${(s.incomplete || []).length ? `<p class="small">Incomplete sessions: ${esc(s.incomplete.map(x => x.day + " (" + x.gaps.map(g => g.reason).join(", ") + ")").join("; "))}</p>` : ""}</section>
    ${trades}`};
}
async function researchSummary() {
  const [a] = await Promise.all([api("/api/analytics"), overviewData()]);
  const o = a.overall, eq = a.equity || {};
  const ts = tradeStats(cache.trades);
  const sample = o.trades < 30 ? ` Only ${plural(o.trades, "trade")} so far: treat every number as early.` : "";
  return {
    lede: `<strong>${esc(`Won ${pct(ts.winRate)} of ${plural(o.trades, "trade")} on fill prices`)}</strong>${esc(` on ${plural(o.trading_days, "trading day")}. Average win ${usd(ts.avgWin)}, average loss ${usd(ts.avgLoss)}.${sample}`)}`,
    body: `<div class="stack">
      ${factList([
        ["Total, from fill prices", `<span class="${tone(o.realized_pnl)}">${esc(usd(o.realized_pnl))}</span>`, "paper money"],
        ["After modeled costs", `<span class="${tone(o.modeled_net_pnl)}">${esc(usd(o.modeled_net_pnl))}</span>`, `${usd(o.cost_per_trade)} per trade`],
        ["Average trade", esc(rmult(o.expectancy_r)), "in units of planned risk"],
        ["Profit factor", esc(ts.profitFactor === null ? NA : fmt(ts.profitFactor, 2)), "gains divided by losses, from fill prices; above 1 makes money"],
        ["Worst drop from a high", esc(usd(eq.max_drawdown)), "after modeled costs"],
        ["Trades hurt by bot errors", esc(`${o.execution_incidents} of ${o.trades}`), "see Mistakes"],
      ])}
      ${a.ratios && !a.ratios.valid ? `<p class="small muted">${esc(cap(a.ratios.reason || "Risk ratios need more trading days."))}</p>` : ""}
      <section>${secHead("Running total after costs", "")}<div class="card pad">${chartSlot(eq.points || [], "cum_net")}</div></section>
    </div>`,
  };
}
async function researchBreakdown(params) {
  const dim = params.dim || "by_side";
  const a = await api("/api/analytics");
  const groups = Object.values((a.breakdowns || {})[dim] || {});
  const max = Math.max(1, ...groups.map(g => Math.abs(n(g.realized_pnl) || 0)));
  const rows = groups.sort((x, y) => (n(x.realized_pnl) || 0) - (n(y.realized_pnl) || 0)).map(g => {
    const v = n(g.realized_pnl) || 0;
    return `<tr><td>${esc(String(g.label).replace(/_/g, " "))}${g.trades < 5 ? ` <span class="small muted">(few trades)</span>` : ""}</td>
      <td class="num">${g.trades}</td><td class="num hide-sm">${esc(pct(g.win_rate))}</td>
      <td class="num ${tone(v)}">${esc(usd(v))}</td>
      <td class="hide-sm" style="width:28%"><div class="bar ${tone(v)}"><i style="width:${(Math.abs(v) / max) * 100}%"></i></div></td>
      <td class="num hide-sm">${esc(rmult(g.mean_r))}</td></tr>`;
  }).join("");
  return {
    body: `
      <nav class="tabs light" aria-label="Group trades by">${DIMENSIONS.map(([k, l]) => `<a href="${href("research", { tab: "breakdown", dim: k })}" aria-current="${k === dim}">${l}</a>`).join("")}</nav>
      ${rows ? `<div class="card table-wrap"><table><thead><tr><th>Group</th><th class="num">Trades</th><th class="num hide-sm">Win rate after costs</th><th class="num">Result</th><th class="hide-sm"><span class="small">Size</span></th><th class="num hide-sm">Avg R</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="note">No trades to group yet.</p>`}
      <p class="small muted" style="margin-top:.75rem">Groups with fewer than 5 trades can swing on a single trade.</p>`,
  };
}
async function researchFunnel(params) {
  const d = await api("/api/opportunities", { day: params.day || "" });
  const stages = (d.stages || []).filter(s => !["signal", "submitted", "filled", "protected", "closed"].includes(s.key));
  const rows = stages.map(s => {
    const w = s.entered ? (s.passed / s.entered) * 100 : 0;
    const top = Array.isArray(s.reasons) ? s.reasons.slice(0, 2) : Object.entries(s.reasons || {}).slice(0, 2);
    const why = top.map(r => Array.isArray(r) ? `${r[0]} (${r[1]})` : (r.reason ? `${r.reason} (${r.count})` : String(r))).join(", ");
    return `<tr><td>${esc(s.label)}${s.failed && why ? `<div class="small muted">${esc(why.replace(/_/g, " "))}</div>` : ""}</td>
      <td class="num">${s.entered}</td><td class="num">${s.passed}</td>
      <td class="hide-sm" style="width:30%"><div class="bar"><i style="width:${w}%"></i></div></td></tr>`;
  }).join("");
  const sm = d.summary || {};
  const days = (d.available_days || []).slice().reverse();
  return {
    lede: esc(sm.diagnosis ? `${sm.diagnosis.headline}. From ${fmt(sm.attempted || 0, 0)} listed stocks, ${sm.signals} gave a signal and ${sm.filled} filled.` : "No scan recorded for this day."),
    controls: `<form class="filters" id="funnel-day"><label class="field">Day<select name="day">${days.map(x => `<option value="${esc(x)}"${x === d.selected ? " selected" : ""}>${esc(longDay(x))}</option>`).join("")}</select></label></form>`,
    body: rows ? `<div class="card table-wrap"><table><thead><tr><th>Check</th><th class="num">In</th><th class="num">Passed</th><th class="hide-sm"><span class="small">Kept</span></th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="note">No checks recorded for this day.</p>`,
  };
}
async function researchBacktest() {
  const d = await api("/api/comparison");
  const p = d.paper, bt = d.backtest || {};
  const ref = (bt.models || []).find(m => m.model === bt.reference_model) || null;
  const row = (label, pv, bv) => `<tr><td>${esc(label)}</td><td class="num">${pv}</td><td class="num">${bv}</td></tr>`;
  return {
    lede: esc(d.banner || ""),
    body: ref ? `<div class="card table-wrap"><table><thead><tr><th>Measure</th><th class="num">Paper (${p.trades} trades)</th><th class="num">Backtest (${esc(ref.trades)} trades)</th></tr></thead><tbody>
      ${row("Win rate", esc(pct(p.win_rate)), esc(pct(ref.win_rate)))}
      ${row("Average trade, in R", esc(rmult(p.expectancy_r)), esc(rmult(ref.expectancy_r)))}
      ${row("Profit factor", esc(p.profit_factor === null ? NA : fmt(n(p.profit_factor), 2)), esc(ref.profit_factor === null ? NA : fmt(n(ref.profit_factor), 2)))}
      ${row("Average win vs loss", esc(p.payoff_ratio === null ? NA : fmt(n(p.payoff_ratio), 2)), esc(ref.payoff === null || ref.payoff === undefined ? NA : fmt(n(ref.payoff), 2)))}
    </tbody></table></div>
    <p class="small muted" style="margin-top:.75rem">Backtest model: ${esc(bt.reference_model)}, as of ${esc(shortDay((bt.as_of || "").slice(0, 10)))}.</p>` : `<p class="note">No backtest results found (${esc(bt.note || "runtime/research")}).</p>`,
  };
}
async function researchPipeline() {
  const d = await api("/api/improvement");
  const s = d.summary, vtag = v => `<span class="fix ${v === "supported" ? "shipped" : v === "rejected" ? "none" : "proposed"}">${esc(v)}</span>`;
  const backlog = d.backlog.map(h => `<tr><td class="num">${esc(h.priority)}</td><td>${esc(h.title)}<br><small class="muted">${esc(h.rationale)}</small></td><td>${esc(h.status)}</td></tr>`).join("");
  const exps = d.experiments.slice(0, 12).map(e => `<tr><td class="nowrap">${esc(shortDay(e.day))}</td><td>${esc(e.hypothesis)}</td><td>${vtag(e.verdict)}</td><td><small>${esc(e.notes || "")}</small></td></tr>`).join("");
  return {
    lede: d.state === "missing" ? esc("No experiment ledger yet. The daily review creates it.")
      : `<strong>${esc(plural(s.promotion_candidates, "promotion candidate"))}.</strong> ${esc(`${plural(s.experiments, "experiment")} run, ${s.rejected} hypotheses rejected, ${s.open} still open. Nothing here changes the live bot; promotion needs your approval in DIVERGENCES.md.`)}`,
    body: `${d.errors.length ? `<p class="note bad" style="margin-bottom:1.25rem">${esc(plural(d.errors.length, "ledger line"))} could not be read: ${esc(d.errors.map(x => `line ${x.line}: ${x.error}`).join("; "))}</p>` : ""}
      <section>${secHead("Research backlog", "Ranked. The pipeline works the top runnable item each trading day.")}
        ${backlog ? `<div class="card table-wrap"><table><thead><tr><th class="num">#</th><th>Hypothesis</th><th>Status</th></tr></thead><tbody>${backlog}</tbody></table></div>` : `<p class="note">Backlog is empty.</p>`}</section>
      <section style="margin-top:1.5rem">${secHead("Experiments", "Newest first. A rerun of the same test needs new evidence.")}
        ${exps ? `<div class="card table-wrap"><table><thead><tr><th>Day</th><th>Hypothesis</th><th>Verdict</th><th>Note</th></tr></thead><tbody>${exps}</tbody></table></div>` : `<p class="note">None yet.</p>`}</section>
      ${d.latest_report ? `<section style="margin-top:1.5rem">${secHead("Latest daily report", esc(shortDay(d.latest_report.day)))}<details class="card pad"><summary>Show report</summary><pre class="evidence">${esc(d.latest_report.text)}</pre></details></section>` : ""}`,
  };
}
function wireResearch(root) {
  const form = $("#funnel-day", root);
  if (form) form.addEventListener("change", () => { go(href("research", { tab: "funnel", day: new FormData(form).get("day") })); });
}

/* ------------------------------------------------------------------ Bot */
async function viewBot() {
  const [h, ov, ops] = await Promise.all([health(true), api("/api/overview"), api("/api/operations")]);
  const st = h.status, rr = (h.safety && h.safety.runtime_report) || {};
  const bs = botState(h);
  const stops = (st.breakers || []).map(b => {
    const [title, how] = breakerWords(b);
    return `<li class="entry"><div class="entry-head"><h3>${esc(title)}${b.symbol ? ` (${esc(b.symbol)})` : ""}</h3><span class="fix ${b.class === "manual" ? "none" : "not_needed"}">${b.class === "manual" ? "Needs you" : "Clears itself"}</span></div>
      <p>${esc(how)}</p><p class="meta">Set at ${esc(etTime(b.ts))} ET. ${esc(b.detail || "")}</p></li>`;
  }).join("");
  const checks = (st.checks || []).map(c => `<tr><td><span class="dot ${c.ok ? "ok" : "bad"}" aria-hidden="true"></span>${esc(c.check)}</td><td class="muted">${esc(c.detail || "")}</td></tr>`).join("");
  const etDay = ts => new Date(ts).toLocaleDateString("en-CA", { timeZone: TZ });
  const steps = (rr.transitions || []).filter(t => etDay(t.at) === st.session).reverse().map(t => `<li><time>${esc(etTime(t.at))}</time><span>${esc(cap(String(t.phase).toLowerCase()))}</span></li>`).join("");
  const sev = { critical: "none", high: "proposed" };
  const queue = (ov.investigation || []).map(i => `<li class="entry"><div class="entry-head"><h3>${esc(i.title)}</h3><span class="fix ${sev[i.severity] || "not_needed"}">${esc(cap(i.severity))}</span></div><p class="meta">${esc(i.detail || "")}</p></li>`).join("");
  return `
  ${band(`
    <p class="kicker-date">Paper account · ${esc(st.session ? longDay(st.session) : "no session")}</p>
    <h1 class="display">${esc(bs.short)}</h1>
    <p class="status"><span class="dot ${bs.cls}" aria-hidden="true"></span><span>${esc(bs.sentence)}</span></p>`)}
  ${body(`<div class="grid">
    <div class="span-7 stack">
      <section>${secHead("Forward collection and daily review", "Recorded paper fills and hypothetical shadow fills remain separate.")}${factList([["Active collection", esc(ops.runtime_mode), ops.collector_heartbeat ? `Heartbeat ${etTime(ops.collector_heartbeat.updated_at)} ET` : "Collector heartbeat unavailable"], ["New PAPER entries", ops.paper_entries_paused ? "Paused" : "Pause file absent; inspect safety gates"], ["Last deterministic review", esc(ops.review?.day || NA), ops.review ? `${ops.review.reconciliation.closed_trips} historical trips; ${ops.review.reconciliation.issues.length} reconciliation issues. Broker snapshot ${etTime(ops.review.observed_at)} ET.` : "Review evidence unavailable"], ["PAPER qualification", "Blocked", "Approvals, supervised acceptance and untouched evidence still required"]])}<p><a href="${href("research", {tab:"paired"})}">Inspect A versus B signals</a></p></section>
      <section>${secHead("Safety stops that are on", "While any is on, the bot opens no new trades. Open positions keep their stops.")}
        ${stops ? `<ul class="entries card">${stops}</ul>` : `<p class="note">None. The bot is free to trade inside its normal hours.</p>`}</section>
      <section>${secHead("Recurring problems", "Patterns found across all recorded trades.")}
        ${queue ? `<ul class="entries card">${queue}</ul>` : `<p class="note">Nothing flagged.</p>`}</section>
    </div>
    <div class="span-5 stack">
      <section>${secHead("Today's timeline", "Eastern time, newest first.")}<ul class="timeline card">${steps || `<li><span class="muted">Nothing recorded yet today.</span></li>`}</ul></section>
      <section>${secHead("Health checks", "")}<div class="card table-wrap"><table class="checks"><tbody>${checks}</tbody></table></div></section>
    </div>
  </div>`)}`;
}

/* ------------------------------------------------------------------ shell */
const VIEWS = {
  overview: { render: () => viewOverview(), nav: "overview" },
  today: { render: p => viewToday(p.params), nav: "today" },
  trades: { render: p => viewTrades(p.params), wire: wireTrades, nav: "trades" },
  trade: { render: p => viewTrade(p.arg), nav: "trades" },
  calendar: { render: p => viewCalendar(p.params), nav: "calendar" },
  mistakes: { render: () => viewMistakes(), nav: "mistakes" },
  research: { render: p => viewResearch(p.params), wire: wireResearch, nav: "research" },
  bot: { render: () => viewBot(), nav: "bot" },
};
const TITLES = { overview: "Overview", today: "Today", trades: "Trades", trade: "Trade", calendar: "Calendar", mistakes: "Mistakes", research: "Research", bot: "The bot" };

/* Navigate; going to the page you're already on reloads it instead of doing nothing. */
function go(target) {
  if (target === location.hash || (target === "#/overview" && !location.hash)) route();
  else location.hash = target;
}

let routeToken = 0;
async function route() {
  const p = parseHash();
  const v = VIEWS[p.view] || VIEWS.overview;
  const app = $("#app");
  const token = ++routeToken;
  CHARTS.length = 0;
  document.querySelectorAll("[data-view]").forEach(a => {
    if (a.dataset.view === v.nav) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  document.title = `${TITLES[p.view] || "Overview"} | Paper P&L`;
  app.setAttribute("aria-busy", "true");
  try {
    const html = await v.render(p);
    if (token !== routeToken) return;
    app.innerHTML = html;
    drawCharts(app);
    if (v.wire) v.wire(app);
  } catch (err) {
    if (token !== routeToken) return;
    app.innerHTML = band(`<h1 class="display">Couldn't load</h1><p class="lede">${esc(err.message)}</p>
      <p class="lede" style="margin-top:.75rem">Check that <code>python -m orb_iex journal-serve</code> is still running, then <a href="${esc(location.hash || "#/today")}" data-reload>reload</a>.</p>`);
  }
  app.removeAttribute("aria-busy");
  window.scrollTo(0, 0);
}

async function paintPill() {
  paintScore();
  try {
    const h = await health(true);
    const bs = botState(h);
    $("#botpill .dot").className = `dot ${bs.cls}`;
    $("#botpill-text").textContent = bs.short;
    $("#botpill").title = bs.sentence;
    $("#foot-updated").textContent = `Checked the bot at ${etTime(new Date().toISOString())} ET.`;
  } catch (err) {
    $("#botpill .dot").className = "dot bad";
    $("#botpill-text").textContent = "Can't reach journal";
    $("#botpill").title = err.message;
  }
}

document.addEventListener("click", ev => {
  if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
  const a = ev.target.closest("a");
  if (a && a.hasAttribute("data-reload")) { ev.preventDefault(); location.reload(); return; }
  if (a && a.hasAttribute("data-back") && history.length > 1) { ev.preventDefault(); history.back(); return; }
  if (a && (a.getAttribute("href") || "").startsWith("#/")) { ev.preventDefault(); go(a.getAttribute("href")); return; }
  const row = ev.target.closest("tr.link");
  if (row && !a) go(row.dataset.href);
});
window.addEventListener("hashchange", route);
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => drawCharts($("#app")), 150); });
paintPill().then(route);
setInterval(paintPill, 30000);

/* Paper Trader dashboard - static single-page app with hash tabs. Loads data.json (written by dashboard.py) and
   re-fetches it every 60 s. Tabs: #overview #portfolio #core #day #risk #markets #sentiment #orders; #asset=XRP opens an asset. */
(() => {
'use strict';
const LC = window.LightweightCharts;
const $ = (s, el = document) => el.querySelector(s), $$ = (s, el = document) => [...el.querySelectorAll(s)];
const TZ = 'Europe/London', REFRESH_MS = 60000;
const TABS = ['overview', 'portfolio', 'core', 'day', 'risk', 'markets', 'sentiment', 'orders'];
const C = {green: '#00ba7c', red: '#f4212e', blue: '#1d9bf0', mut: '#71767b', txt: '#e7e9ea', grid: '#16181c', line: '#2f3336', grey: '#536471'};
let D = null, lastGen = null, loadErr = null;
const S = {tab: 'overview', aSort: {k: 'a', d: 1}, tSort: {k: 't', d: -1}, pSort: {k: 'value', d: -1}, rSort: {k: 'score', d: -1},
  rcSort: {k: 'comb', d: -1}, rsSort: {k: 'score', d: -1},
  filter: 'all', q: '', eqRange: 'ALL', ccy: 'gbp', ptype: 'candle', dRange: 'ALL', open: null,
  tAsset: '', tSide: '', tSleeve: '', tFrom: '', tTo: '', tLimit: 50, sLimit: 15, sAsset: '', pPot: '', rTheme: ''};
const POT = {core: 'Core pot', day: 'Day pot', risk: 'Risk pot'};
const POTS = ['core', 'day', 'risk'];
const potTag = s => s ? `<span class="tag ${s === 'core' ? 'blue' : s === 'risk' ? 'risk' : ''}">${esc(POT[s] || s)}</span>` : '';
const potCash = () => POTS.reduce((a, k) => a + ((D.sleeves[k] || {}).cash || 0), 0);
const charts = {eq: null, d: null, dsig: null, dsent: null};

/* ---------- formatting ---------- */
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const isNum = v => typeof v === 'number' && isFinite(v);
const dfmt = new Intl.DateTimeFormat('en-GB', {timeZone: TZ, weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false});
const dfmtS = new Intl.DateTimeFormat('en-GB', {timeZone: TZ, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false});
const dlong = new Intl.DateTimeFormat('en-GB', {timeZone: TZ, weekday: 'long', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false});
const tfmt = new Intl.DateTimeFormat('en-GB', {timeZone: TZ, hour: '2-digit', minute: '2-digit', hour12: false});
const dayfmt = new Intl.DateTimeFormat('en-GB', {timeZone: TZ, day: 'numeric', month: 'short'});
const ymd = new Intl.DateTimeFormat('en-CA', {timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit'});
const fT = t => isNum(t) ? dfmt.format(t * 1000) : '-';
const fTs = t => isNum(t) ? dfmtS.format(t * 1000) : '-';
const dec = p => { const a = Math.abs(p); return !a ? 2 : a >= 1 ? 2 + (a < 10 ? 2 : 0) : Math.min(8, 2 + Math.ceil(-Math.log10(a)) + 2); };
const fP = (p, sym = '£') => isNum(p) ? sym + p.toLocaleString('en-GB', {minimumFractionDigits: Math.min(dec(p), 2), maximumFractionDigits: dec(p)}) : '-';
const fM = (v, sign = false) => isNum(v) ? (sign ? (v > 0.0049 ? '+' : v < -0.0049 ? '-' : '') : (v < 0 ? '-' : '')) + '£' + Math.abs(v).toLocaleString('en-GB', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '-';
const fM0 = v => isNum(v) ? '£' + Math.round(v).toLocaleString('en-GB') : '-';
const fPct = (v, d = 2, sign = true) => isNum(v) ? (sign && v > 0.004 ? '+' : '') + v.toFixed(d) + '%' : '-';
const fGas = v => isNum(v) ? (v === 0 ? '£0' : '£' + (v < 0.01 ? v.toPrecision(2) : v.toFixed(3))) : '-';
const fFee = v => isNum(v) ? '£' + v.toFixed(v < 1 ? 3 : 2) : '-';
const fQty = v => isNum(v) ? (Math.abs(v) >= 1000 ? v.toLocaleString('en-GB', {maximumFractionDigits: 2}) : Math.abs(v) >= 1 ? v.toLocaleString('en-GB', {maximumFractionDigits: 4}) : v.toPrecision(4)) : '-';
const cls = v => isNum(v) ? (v > 0.0049 ? 'pos' : v < -0.0049 ? 'neg' : '') : '';
const ago = t => { if (!isNum(t)) return '-'; const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? 'just now' : s < 3600 ? Math.round(s / 60) + ' min ago' : s < 86400 ? (s / 3600).toFixed(1) + ' h ago' : Math.round(s / 86400) + ' d ago'; };
const until = t => { if (!isNum(t)) return ''; const s = t - Date.now() / 1000; if (s <= 0) return 'due now';
  return s < 3600 ? `in ${Math.round(s / 60)} min` : s < 86400 ? `in ${(s / 3600).toFixed(0)} h` : `in ${(s / 86400).toFixed(1)} days`; };
const safeUrl = u => /^https?:\/\//i.test(u || '') ? u : null;
const host = u => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return 'link'; } };
const stat = (l, v, c = '', s = '') => `<div class="stat"><div class="l">${esc(l)}</div><div class="v ${c}">${v}</div>${s ? `<div class="s">${s}</div>` : ''}</div>`;

/* ---------- chart helpers ---------- */
function mkChart(el, opts = {}) {
  return LC.createChart(el, Object.assign({
    autoSize: true,
    layout: {background: {type: 'solid', color: 'transparent'}, textColor: C.mut, fontSize: 12, fontFamily: 'Inter,-apple-system,system-ui,sans-serif'},
    grid: {vertLines: {visible: false}, horzLines: {color: C.grid}},
    rightPriceScale: {borderVisible: false},
    timeScale: {borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 2,
      tickMarkFormatter: (t, type) => type <= 2 ? dayfmt.format(t * 1000) : tfmt.format(t * 1000)},
    localization: {locale: 'en-GB', timeFormatter: t => fT(t)},
    crosshair: {mode: LC.CrosshairMode.Normal, vertLine: {color: C.line, labelBackgroundColor: '#202327'}, horzLine: {color: C.line, labelBackgroundColor: '#202327'}},
    handleScroll: {mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false},
    handleScale: {mouseWheel: true, pinch: true, axisPressedMouseMove: true},
  }, opts));
}
const pfmt = p => { const pr = Math.min(dec(p || 1), 8); return {type: 'price', precision: pr, minMove: Math.pow(10, -pr)}; };
function setRange(chart, times, r) {
  if (!chart || !times.length) return;
  const last = times[times.length - 1], span = {'1D': 86400, '3D': 3 * 86400, '1W': 7 * 86400}[r];
  if (!span || last - times[0] <= span) { chart.timeScale().fitContent(); return; }
  try { chart.timeScale().setVisibleRange({from: last - span, to: last}); } catch (e) { chart.timeScale().fitContent(); }
}
function dropChart(k) { if (charts[k]) { charts[k].remove(); charts[k] = null; } }
function emptyMsg(id, msg) { const e = $('#' + id); e.classList.toggle('hidden', !msg); e.textContent = msg || ''; }
function seg(id, attr, val) { $$('#' + id + ' button').forEach(b => b.classList.toggle('on', b.dataset[attr] === val)); }

/* ---------- data helpers ---------- */
const assetBy = a => (D.assets || []).find(x => x.a === a);
const posAll = a => (D.positions || []).filter(x => x.asset === a);
const posBy = a => { const l = posAll(a); return l.find(x => x.sleeve === 'day') || l.find(x => x.sleeve === 'risk') || l[0]; };
const posVal = a => { const l = posAll(a); return l.length ? l.reduce((s, p) => s + (p.value || 0), 0) : null; };
const themeOf = a => { const r = ((D.sleeves.core || {}).ranking || []).find(x => x.a === a); return r ? r.theme_label : (assetBy(a) || {}).theme || null; };
const kindOf = a => { const x = assetBy(a); return x ? x.k : /x$/.test(a) ? 'stock' : 'crypto'; };
const assetCell = (a, sub) => `<div class="asset"><span class="ic">${esc(a.replace(/x$/, '').slice(0, 4))}</span><div><div class="nm">${esc(a)}</div><div class="sb">${esc(sub ?? (kindOf(a) === 'stock' ? (themeOf(a) || 'Stock') : 'Crypto'))}</div></div></div>`;
const assetLink = a => `<a href="#asset=${encodeURIComponent(a)}" class="nm" style="color:var(--hi);font-weight:700">${esc(a)}</a>`;
function fxAt(t) {
  const h = D.fx_hist || []; if (!h.length) return D.fx.rate;
  let lo = 0, hi = h.length - 1, r = h[0][1];
  while (lo <= hi) { const m = (lo + hi) >> 1; if (h[m][0] <= t) { r = h[m][1]; lo = m + 1; } else hi = m - 1; }
  return r || D.fx.rate;
}
function sentLatest(a) { return (D.sentiment || []).find(s => s.a === a) || null; }
function scoreBar(v, txt) {
  if (!isNum(v)) return '<span class="muted">-</span>';
  const w = Math.min(50, Math.abs(v) * 50), col = v >= 0 ? C.green : C.red;
  return `<span class="score"><span class="sbar"><span style="left:${v >= 0 ? 50 : 50 - w}%;width:${w}%;background:${col}"></span></span><span class="${cls(v)}">${v >= 0 ? '+' : ''}${v.toFixed(2)}</span>${txt ? ' ' + txt : ''}</span>`;
}
function spark(vals) {
  const v = (vals || []).filter(isNum); if (v.length < 2) return '<span class="muted">-</span>';
  const W = 96, H = 28, mn = Math.min(...v), mx = Math.max(...v), rg = mx - mn || 1;
  const pts = v.map((x, i) => `${(i / (v.length - 1) * W).toFixed(1)},${(H - 2 - (x - mn) / rg * (H - 4)).toFixed(1)}`).join(' ');
  const col = v[v.length - 1] >= v[0] ? C.green : C.red;
  return `<svg class="spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><polyline fill="none" stroke="${col}" stroke-width="1.6" points="${pts}"/></svg>`;
}
const sideTag = a => a ? `<span class="tag ${a === 'BUY' ? 'buy' : a === 'SELL' ? 'sell' : a === 'TRANSFER' ? 'xfer' : ''}">${esc(a === 'BUY' ? 'Buy' : a === 'SELL' ? 'Sell' : a === 'HOLD' ? 'Hold' : a === 'SKIP' ? 'Skip' : a === 'TRANSFER' ? 'Transfer' : a)}</span>` : '<span class="muted">-</span>';
const pnlCell = (v, pct) => isNum(v) ? `<span class="${cls(v)}">${fM(v, true)}${isNum(pct) ? ` <span style="opacity:.85">(${fPct(pct)})</span>` : ''}</span>` : '<span class="muted">-</span>';

/* ---------- generic sortable table ---------- */
function table(el, cols, rows, st, onSort, opts = {}) {
  const sorted = rows.slice();
  const col = st && cols.find(c => c.k === st.k);
  if (col) {
    const sv = col.sv || (r => r[col.k]);
    sorted.sort((a, b) => { const x = sv(a), y = sv(b);
      if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
      return (typeof x === 'string' ? x.localeCompare(y) : x - y) * st.d; });
  }
  const th = cols.map(c => `<th${c.nosort || !st ? '' : ` data-k="${c.k}"`} class="${c.num ? 'num' : ''} ${st && st.k === c.k ? 'sorted' : ''}">${esc(c.l)}${st && st.k === c.k ? `<span class="ar">${st.d > 0 ? '▲' : '▼'}</span>` : ''}</th>`).join('');
  const shown = opts.limit ? sorted.slice(0, opts.limit) : sorted;
  const body = shown.length ? shown.map(r => `<tr${opts.key ? ` data-id="${esc(opts.key(r))}"` : ''}>` +
      cols.map(c => `<td class="${c.num ? 'num' : ''} ${c.wrap ? 'wrap' : ''}">${c.f ? c.f(r) : esc(r[c.k])}</td>`).join('') + '</tr>').join('')
    : `<tr><td class="emptyrow" colspan="${cols.length}">${esc(opts.empty || 'Nothing to show')}</td></tr>`;
  el.innerHTML = `<thead><tr>${th}</tr></thead><tbody>${body}${shown.length && opts.foot ? opts.foot : ''}</tbody>`;
  if (st) $$('th[data-k]', el).forEach(h => h.onclick = () => { const k = h.dataset.k;
    if (st.k === k) st.d = -st.d; else { st.k = k; st.d = cols.find(c => c.k === k).asc ? 1 : -1; } onSort(); });
  if (opts.onRow) $$('tbody tr[data-id]', el).forEach(tr => tr.onclick = e => { if (e.target.closest('a')) return; opts.onRow(tr.dataset.id); });
}

/* ---------- header ---------- */
function renderHeader() {
  const age = Date.now() / 1000 - (D ? D.updated : 0);
  $('#updated-ago').textContent = D ? `Updated ${ago(D.updated)}` : (loadErr ? 'Data unavailable' : 'Loading…');
  $('#live-dot').className = 'dot ' + (!D ? 'bad' : age > 3600 ? 'bad' : age > 1500 ? 'warn' : 'ok');
  const al = $('#alert'); let msg = '';
  if (loadErr && !D) msg = 'Could not load data.json: ' + loadErr;
  else if (D && age > 3600) msg = `Data is ${ago(D.updated).replace(' ago', '')} old - the 15-minute update may have stalled.`;
  else if (loadErr) msg = 'Last refresh failed (' + loadErr + '); showing the previous data.';
  else if (D && D.risk.kill) msg = 'Kill switch is ON - all trading is paused.';
  al.textContent = msg; al.classList.toggle('hidden', !msg);
}

/* ---------- overview ---------- */
function statusWords() {
  const r = D.risk, d = D.sleeves.day || {};
  if (r.kill) return {txt: 'Paused (kill switch on)', c: 'neg'};
  if (d.halted) return {txt: 'Day trading halted for today (daily loss limit hit)', c: 'neg'};
  if ((D.sleeves.risk || {}).halted) return {txt: 'Risk pot paused (30% drawdown limit) - waiting for review', c: 'neg'};
  if (r.fx_stale) return {txt: 'New buys paused (exchange rate is stale)', c: 'warnc'};
  return {txt: 'All normal', c: 'pos'};
}
function renderOverview() {
  const s = D.stats, Z = D.sleeves, start = D.start;
  $$('.js-start').forEach(e => e.textContent = start.toFixed(0));
  const pnl = s.equity - start;
  $('#ov-value').textContent = fM(s.equity);
  $('#ov-pnl').innerHTML = `<span class="${cls(pnl)}">${fM(pnl, true)} (${fPct(s.ret)})</span><span class="muted">all time</span>`;
  const inv = (D.positions || []).reduce((a, p) => a + (p.value || 0), 0), cash = potCash();
  const fees = (D.gas.fees || 0) + (D.gas.paid || 0);
  $('#ov-stats').innerHTML = stat("Today's P&L", `<span class="${cls(s.day_pnl)}">${fM(s.day_pnl, true)}</span>`, '', `${fPct(s.day_pnl_pct)} since midnight UK`) +
    stat('Cash', fM(cash), '', 'ready to invest') + stat('Invested', fM(inv), '', `${(D.positions || []).length} holdings`) +
    stat('Fees & gas paid', fM(fees), '', `incl. ${fGas(D.gas.paid)} network gas`);
  // cards
  const btcV = (D.equity.btc || []).filter(isNum).slice(-1)[0];
  const diff = isNum(btcV) ? s.equity - btcV : null;
  const btcWords = !isNum(btcV) ? 'Not enough data yet to compare with Bitcoin.' :
    `If you had put £${start.toFixed(0)} straight into Bitcoin at the start, it would now be worth <b>${fM(btcV)}</b>. ` +
    (Math.abs(diff) < 0.005 ? 'Your account is <b>level</b> with it.' : `Your account is <b class="${cls(diff)}">${fM(Math.abs(diff))} ${diff > 0 ? 'ahead of' : 'behind'}</b> Bitcoin.`);
  const st = statusWords(), d = Z.day || {}, core = Z.core || {};
  const dayLine = d.flat_now ? 'Day pot: closed for the night (re-opens 08:00 UK).' : d.entries_open ? `Day pot: open for new trades until ${esc((d.rules || {}).cutoff)} UK; everything is sold by ${esc((d.rules || {}).flatten)} UK.` : `Day pot: no new trades now; everything is sold by ${esc((d.rules || {}).flatten)} UK.`;
  const ip = s.equity ? inv / s.equity * 100 : 0;
  $('#ov-cards').innerHTML =
    `<div class="card"><div class="lbl">Cash vs invested</div><div class="bar2"><span style="width:${ip.toFixed(1)}%;background:var(--blue)"></span><span style="width:${(100 - ip).toFixed(1)}%;background:#536471"></span></div>` +
    `<p class="plain"><b>${fM(inv)}</b> (${ip.toFixed(0)}%) is invested and <b>${fM(cash)}</b> is cash. The Core pot invests its cash gradually; the Risk pot only buys on breakouts, so it can sit in cash for a while.</p></div>` +
    `<div class="card"><div class="lbl">Compared with Bitcoin</div><p class="plain" style="margin-top:10px">${btcWords}</p></div>` +
    `<div class="card"><div class="lbl">Status</div><p class="plain" style="margin-top:10px"><b class="${st.c}">${esc(st.txt)}.</b> ${dayLine} Next Core rebalance: <b>${esc(dlong.format((core.next_rebalance || 0) * 1000))}</b> UK.</p></div>`;
  // pots
  const pot = (k, x, desc) => `<a class="pot" href="#${k}"><div><div class="pot-n">${POT[k]}</div><div class="pot-d">${desc}</div></div>` +
    `<div class="pot-v"><b>${fM(x.equity)}</b><span class="${cls(x.pnl)}">${fM(x.pnl, true)} (${fPct(x.ret)})</span></div></a>`;
  $('#ov-pots').innerHTML = pot('core', core, `Long-term holdings · started ${fM0(core.start)} · ${core.open_n} holdings, ${fM(core.cash)} cash`) +
    pot('day', d, `Short-term trades, flat by ${esc((d.rules || {}).flatten)} UK · started ${fM0(d.start)} · ${d.open_n} open`) +
    (Z.risk ? pot('risk', Z.risk, `Speculative small caps & niche stocks · started ${fM0(Z.risk.start)} · ${Z.risk.open_n} of ${(Z.risk.rules || {}).max_open} open${Z.risk.halted ? ' · <b class="neg">paused</b>' : ''}`) : '') +
    ((D.transfers || []).length ? `<div class="note">Pot split ${POTS.map(k => (Z[k] || {}).pct + '%').join(' / ')} (Core / Day / Risk). Cash moved between pots: ${(D.transfers || []).map(x => `${fTs(x.t)} ${fM(x.amount)} ${esc(POT[x.frm] || x.frm)} → ${esc(POT[x.to] || x.to)}`).join(' · ')} (not counted as profit or loss).</div>` : '');
  const cols = [{k: 't', l: 'Time', f: r => esc(fTs(r.t))}, {k: 'side', l: 'Side', f: r => sideTag(r.side)}, {k: 'asset', l: 'Asset', f: r => assetLink(r.asset)},
    {k: 'value', l: 'Value', num: 1, f: r => fM(r.value)}, {k: 'pnl', l: 'P&L', num: 1, f: r => pnlCell(r.pnl)}];
  table($('#ov-orders'), cols, (D.trades || []).slice(0, 6), null, null, {empty: 'No orders yet'});
  renderRisk();
}
let eqSeries = null, startLine = null;
function renderEquity() {
  if (!LC) return;
  const E = D.equity, el = $('#eq-chart');
  if (!charts.eq) {
    charts.eq = mkChart(el, {rightPriceScale: {borderVisible: false, scaleMargins: {top: .15, bottom: .08}}});
    eqSeries = charts.eq.addAreaSeries({lineColor: C.blue, topColor: 'rgba(29,155,240,.28)', bottomColor: 'rgba(29,155,240,0)', lineWidth: 2,
      priceFormat: {type: 'custom', formatter: v => '£' + v.toFixed(2)}, lastValueVisible: true, priceLineVisible: false});
    const tip = $('#eq-tip');
    charts.eq.subscribeCrosshairMove(p => {
      if (!p.time || !p.point || p.point.x < 0 || p.point.y < 0) { tip.classList.add('hidden'); return; }
      const x = p.seriesData.get(eqSeries), v = x && x.value; if (!isNum(v)) { tip.classList.add('hidden'); return; }
      const i = E.t.indexOf(p.time), b = i >= 0 ? E.btc[i] : null;
      tip.innerHTML = `<div class="muted">${esc(fT(p.time))}</div><div class="row"><span>Account value</span><b>${fM(v)}</b></div>` +
        `<div class="row"><span>Since start</span><b class="${cls(v - D.start)}">${fM(v - D.start, true)} (${fPct((v / D.start - 1) * 100)})</b></div>` +
        (isNum(b) ? `<div class="row"><span>Bitcoin instead</span><b>${fM(b)}</b></div>` : '');
      tip.classList.remove('hidden');
      const w = el.clientWidth, tw = tip.offsetWidth;
      tip.style.left = Math.min(Math.max(p.point.x + 14, 4), w - tw - 4) + 'px'; tip.style.top = '8px';
    });
    el.addEventListener('dblclick', () => { S.eqRange = 'ALL'; seg('eq-range', 'r', 'ALL'); charts.eq.timeScale().fitContent(); });
  }
  eqSeries.applyOptions({pointMarkersVisible: E.t.length < 40});
  eqSeries.setData(E.t.map((t, i) => ({time: t, value: E.eq[i]})).filter(p => isNum(p.value)));
  if (startLine) eqSeries.removePriceLine(startLine);
  startLine = eqSeries.createPriceLine({price: D.start, color: '#536471', lineStyle: LC.LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: 'start'});
  setRange(charts.eq, E.t, S.eqRange);
  emptyMsg('eq-empty', E.t.length ? '' : 'No points yet - one is added every 15 minutes.');
  $('#eq-note').textContent = E.t.length < 8 ? `${E.t.length} point${E.t.length === 1 ? '' : 's'} so far; a new one is added every 15 minutes.` : `Since ${fTs(E.t[0])} UK · drag to pan, scroll or pinch to zoom, double-click to reset.`;
}

/* ---------- portfolio ---------- */
const PAL = ['#1d9bf0', '#8ecdf8', '#0f6fb0', '#c4e4fb', '#4fb3f4', '#0a4a75', '#a9b4bf', '#6a8fae', '#d6dde3', '#3b6e96'];
function renderPortfolio() {
  const Z = D.sleeves, P = (D.positions || []).filter(p => !S.pPot || p.sleeve === S.pPot);
  const total = D.stats.equity || 1, cashT = S.pPot ? (Z[S.pPot].cash || 0) : potCash();
  const inv = P.reduce((a, p) => a + (p.value || 0), 0), unr = P.reduce((a, p) => a + (p.pnl || 0), 0), cost = P.reduce((a, p) => a + (p.cost || 0), 0);
  $('#pf-stats').innerHTML = stat(S.pPot ? POT[S.pPot] + ' value' : 'Total value', fM(S.pPot ? Z[S.pPot].equity : D.stats.equity), '', S.pPot ? '' : POTS.map(k => `${POT[k].replace(' pot', '')} ${fM((Z[k] || {}).equity)}`).join(' · ')) +
    stat('Cash', fM(cashT), '', S.pPot ? '' : POTS.map(k => `${POT[k].replace(' pot', '')} ${fM((Z[k] || {}).cash)}`).join(' · ')) + stat('Holdings value', fM(inv), '', `${P.length} position${P.length === 1 ? '' : 's'}`) +
    stat('Unrealised P&L', `<span class="${cls(unr)}">${fM(unr, true)}</span>`, '', cost ? fPct(unr / cost * 100) + ' on cost' : '') +
    stat('Realised P&L', `<span class="${cls(D.stats.realized)}">${fM(S.pPot ? Z[S.pPot].realized : D.stats.realized, true)}</span>`, '', 'from closed trades');
  const cols = [
    {k: 'asset', l: 'Asset', asc: 1, f: r => assetCell(r.asset)},
    {k: 'sleeve', l: 'Pot', asc: 1, f: r => potTag(r.sleeve)},
    {k: 'qty', l: 'Quantity', num: 1, f: r => fQty(r.qty)},
    {k: 'entry', l: 'Avg buy price', num: 1, f: r => fP(r.entry)},
    {k: 'now', l: 'Current price', num: 1, f: r => fP(r.now)},
    {k: 'value', l: 'Value', num: 1, f: r => `<b style="color:var(--hi)">${fM(r.value)}</b>`},
    {k: 'pnl', l: 'P&L £', num: 1, f: r => `<span class="${cls(r.pnl)}">${fM(r.pnl, true)}</span>`},
    {k: 'pnl_pct', l: 'P&L %', num: 1, f: r => `<span class="${cls(r.pnl_pct)}">${fPct(r.pnl_pct)}</span>`},
    {k: 'alloc', l: 'Allocation', num: 1, sv: r => r.value, f: r => isNum(r.value) ? (r.value / total * 100).toFixed(1) + '%' : '-'},
  ];
  const foot = `<tr><td>${assetCell('Cash', 'GBP')}</td><td>${S.pPot ? potTag(S.pPot) : '<span class="muted">all pots</span>'}</td><td></td><td></td><td></td><td class="num"><b style="color:var(--hi)">${fM(cashT)}</b></td><td></td><td></td><td class="num">${(cashT / total * 100).toFixed(1)}%</td></tr>` +
    `<tr class="total"><td>Total</td><td></td><td></td><td></td><td></td><td class="num">${fM(inv + cashT)}</td><td class="num ${cls(unr)}">${fM(unr, true)}</td><td></td><td class="num">${((inv + cashT) / total * 100).toFixed(1)}%</td></tr>`;
  table($('#pf-table'), cols, P, S.pSort, renderPortfolio, {empty: 'No holdings yet', foot, key: r => r.asset, onRow: openAsset});
  // donut (by asset, both pots combined unless filtered) + cash
  const by = {}; P.forEach(p => { by[p.asset] = (by[p.asset] || 0) + (p.value || 0); });
  let sl = Object.entries(by).sort((a, b) => b[1] - a[1]);
  if (sl.length > 9) { const rest = sl.slice(8).reduce((a, x) => a + x[1], 0); sl = sl.slice(0, 8).concat([['Other', rest]]); }
  sl = sl.map(([n, v], i) => ({n, v, c: PAL[i % PAL.length]}));
  if (cashT > 0.005) sl.push({n: 'Cash', v: cashT, c: '#2f3336'});
  const sum = sl.reduce((a, x) => a + x.v, 0) || 1, R = 80, CX = 100, CY = 100, SW = 26, circ = 2 * Math.PI * R;
  let off = 0;
  const arcs = sl.map(x => { const len = x.v / sum * circ, g = sl.length > 1 ? Math.min(1.5, len / 3) : 0;
    const a = `<circle cx="${CX}" cy="${CY}" r="${R}" fill="none" stroke="${x.c}" stroke-width="${SW}" stroke-dasharray="${Math.max(len - g, 0.01)} ${circ}" stroke-dashoffset="${-off}" transform="rotate(-90 ${CX} ${CY})"><title>${esc(x.n)} ${fM(x.v)}</title></circle>`;
    off += len; return a; }).join('');
  $('#pf-donut').innerHTML = `<svg viewBox="0 0 200 200" role="img" aria-label="Allocation">${arcs}<text x="100" y="94" text-anchor="middle" fill="#71767b" font-size="12">${S.pPot ? esc(POT[S.pPot]) : 'Total'}</text>` +
    `<text x="100" y="116" text-anchor="middle" fill="#fff" font-size="20" font-weight="800">${esc(fM(sum))}</text></svg>`;
  $('#pf-legend').innerHTML = sl.map(x => `<div class="li"><i style="background:${x.c}${x.n === 'Cash' ? ';border:1px solid #536471' : ''}"></i><span>${esc(x.n)}</span><span class="vv">${fM(x.v)}</span><span class="pc">${(x.v / sum * 100).toFixed(1)}%</span></div>`).join('');
}

/* ---------- core ---------- */
function renderCore() {
  const c = D.sleeves.core || {}, R = c.rules || {}, T = c.targets || [];
  const crypto = T.filter(t => kindOf(t.a) !== 'stock'), stocks = T.filter(t => kindOf(t.a) === 'stock');
  const cp = crypto.reduce((a, t) => a + t.target, 0);
  $('#core-intro').innerHTML = `Long-term pot that started with <b>${fM0(c.start)}</b>. About <b>${cp.toFixed(0)}% crypto</b> (${crypto.map(t => `${esc(t.a)} ${t.target.toFixed(0)}%`).join(', ')}) ` +
    `and <b>${(c.stock_pct || 0).toFixed(0)}% stocks</b>, split equally across ${stocks.length || 'the'} stocks picked every ${esc(R.rebalance || 'Monday')} from a watchlist of ` +
    `${(c.universe || []).reduce((a, u) => a + u.tickers.length, 0)} growth stocks in ${(c.universe || []).length} themes (max ${c.max_per_theme} per theme). ` +
    `New money goes in gradually - up to ${R.tranches} pieces per holding, at most ${R.per_day} per holding per day, preferring dips. Each holding has a ${R.stop}% stop-loss; there is no profit-taking, the weekly rebalance trims winners that grow more than ${R.band} points above target.`;
  const built = T.filter(t => t.status === 'built').length, inv = T.reduce((a, t) => a + (t.value || 0), 0);
  $('#core-stats').innerHTML = stat('Core pot value', fM(c.equity), '', `<span class="${cls(c.pnl)}">${fM(c.pnl, true)} (${fPct(c.ret)})</span>`) +
    stat('Invested', fM(inv), '', `${fM(c.cash)} cash still to invest`) +
    stat('Build progress', `${built} / ${T.length}`, '', `holdings fully built · ${c.equity ? (inv / c.equity * 100).toFixed(0) : 0}% invested`) +
    stat('Next rebalance', esc(fTs(c.next_rebalance)), '', `${until(c.next_rebalance)} · re-picks stocks`) +
    stat('Stocks last picked', esc(fTs(c.picks_ts)), '', esc(c.picks_source || ''));
  const mx = Math.max(1, ...T.map(t => Math.max(t.target, t.weight))) * 1.08;
  $('#core-targets').innerHTML = T.length ? T.map(t => {
    const st = t.status === 'built' ? 'built' : t.status === 'stopped' ? 'stopped out' : `building ${t.n}/${R.tranches}`;
    return `<div class="tg-row"><div class="nm">${esc(t.a)}<small>${esc(kindOf(t.a) === 'stock' ? (themeOf(t.a) || 'Stock') : 'Crypto')}</small></div>` +
      `<div class="tg-bar" title="actual ${t.weight}% vs target ${t.target}%"><span class="a" style="width:${(t.weight / mx * 100).toFixed(1)}%"></span><span class="t" style="left:${(t.target / mx * 100).toFixed(1)}%"></span></div>` +
      `<div class="num">${t.weight.toFixed(1)}% <span class="muted">/ ${t.target.toFixed(1)}%</span><small>${fM(t.value)} · ${esc(st)}</small></div></div>`; }).join('') +
    '<div class="note">Blue bar = actual share of the Core pot · white mark = target.</div>' : '<div class="muted">No targets yet.</div>';
  const info = c.picks_info || {}, picks = c.picks || [];
  $('#picks-sub').textContent = `${picks.length} stocks, ${c.stock_pct}% of the pot split equally (each ~${fM((c.equity || 0) * (c.stock_pct || 0) / 100 / Math.max(picks.length, 1))}). Number of picks is fitted so each one is at least £${c.min_pick} (min trade £${D.risk.limits.min_trade}).`;
  const same = (c.would_pick || []).join(',') === picks.join(',');
  $('#core-picks').innerHTML = (picks.length ? picks.map(a => { const r = info[a] || {}, live = (c.ranking || []).find(x => x.a === a) || r;
    return `<div class="pick"><div class="pick-h"><span><span class="nm">${assetLink(a)}</span> <span class="tag">${esc(r.theme_label || themeOf(a) || '')}</span></span>` +
      `<span>${scoreBar(live.score)}</span></div><div class="why">Why: ${esc(r.why || '-')}</div></div>`; }).join('') : '<div class="muted">No stock picks (none eligible).</div>') +
    (!same && (c.would_pick || []).length ? `<div class="note">If the stocks were re-picked right now: <b>${(c.would_pick || []).map(esc).join(', ')}</b> (applied at the next rebalance).</div>` : '');
  // plans
  $('#plans').innerHTML = (c.plans || []).map(p => {
    const rows = p.rows.filter(r => r.buy > 0), skip = p.rows.filter(r => r.skip).length;
    return `<div class="plan"><h3>+${fM0(p.topup)}</h3><div class="sub">Core pot becomes ${fM(p.core_after)} · ${p.n_picks} stocks (~${fM(p.per_pick)} each) · ${fM(p.deploy)} to invest incl. current cash</div>` +
      `<div class="themes">${p.themes.map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>` +
      `<table><tbody>${rows.map(r => `<tr><td>${assetLink(r.a)} <span class="muted">${esc(r.kind === 'stock' ? r.theme || '' : 'crypto')}</span></td><td class="num">${r.target_pct.toFixed(1)}%</td><td class="num"><b>${fM(r.buy)}</b></td></tr>`).join('')}` +
      `<tr class="total"><td>Total to buy</td><td></td><td class="num">${fM(p.buy_total)}</td></tr></tbody></table>` +
      (skip ? `<div class="note">${skip} small top-up${skip > 1 ? 's' : ''} under £${D.risk.limits.min_trade} skipped.</div>` : '') + '</div>'; }).join('') || '<div class="muted">No plans yet (needs stock price history).</div>';
  // ranking
  const sel = $('#rank-theme'), th = (c.universe || []).map(u => u.label);
  if ([...sel.options].length !== th.length + 1) { sel.innerHTML = '<option value="">All themes</option>' + th.map(t => `<option>${esc(t)}</option>`).join(''); sel.value = S.rTheme; }
  const rows = (c.ranking || []).filter(r => !S.rTheme || r.theme_label === S.rTheme).map((r, i) => Object.assign({}, r));
  const order = (c.ranking || []).filter(r => r.eligible).map(r => r.a);
  const pc = v => isNum(v) ? `<span class="${cls(v)}">${fPct(v * 100, 0)}</span>` : '<span class="muted">n/a</span>';
  const cols = [
    {k: 'rk', l: '#', num: 1, sv: r => { const i = order.indexOf(r.a); return i < 0 ? 999 : i; }, asc: 1, f: r => { const i = order.indexOf(r.a); return i < 0 ? '<span class="muted">-</span>' : String(i + 1); }},
    {k: 'a', l: 'Stock', asc: 1, f: r => assetCell(r.a, r.theme_label)},
    {k: 'score', l: 'Score', f: r => scoreBar(r.score)},
    {k: 'r6', l: '6 months', num: 1, f: r => pc(r.r6)}, {k: 'r3', l: '3 months', num: 1, f: r => pc(r.r3)}, {k: 'r1', l: '1 month', num: 1, f: r => pc(r.r1)},
    {k: 'quality', l: 'Trend quality', num: 1, f: r => `<span class="${cls(r.quality)}">${r.quality >= 0 ? '+' : ''}${r.quality.toFixed(1)}</span>`},
    {k: 'sent', l: 'Sentiment', num: 1, f: r => isNum(r.sent) ? `<span class="${cls(r.sent)}">${r.sent >= 0 ? '+' : ''}${r.sent.toFixed(2)}</span>` : '<span class="muted">-</span>'},
    {k: 'vol', l: 'Daily move', num: 1, f: r => (r.vol * 100).toFixed(1) + '%'},
    {k: 'st', l: 'Status', sv: r => r.pick ? 0 : r.eligible ? 1 : 2, asc: 1, f: r => r.pick ? '<span class="tag solid">Picked</span>' : r.eligible ? '<span class="tag blue">Eligible</span>' : `<span class="muted small">${esc(r.not_eligible)}</span>`},
  ];
  table($('#rank-table'), cols, rows, S.rSort, renderCore, {empty: 'No ranking yet - daily prices are still loading', key: r => r.a, onRow: openAsset, limit: S.rAll ? 0 : 20});
  $('#rank-more').innerHTML = rows.length > 20 ? `<button class="btn ghost" id="rank-toggle">${S.rAll ? 'Show top 20 only' : `Show all ${rows.length}`}</button>` : '';
  const rt = $('#rank-toggle'); if (rt) rt.onclick = () => { S.rAll = !S.rAll; renderCore(); };
  const w = c.weights || {};
  $('#rank-sub').innerHTML = `Score from -1 to +1 = ${Math.round((w.momentum || 0) * 100)}% medium-term momentum (6, 3 and 1-month returns adjusted for how much the stock swings) + ` +
    `${Math.round((w.quality || 0) * 100)}% trend quality (above its 50-day average, average rising, near its 6-month high) + ${Math.round((w.sentiment || 0) * 100)}% news sentiment when available. ` +
    `To be eligible a stock must be up over 3 months and above its 50-day average. ${order.length} of ${(c.ranking || []).length} are eligible today.`;
  const L = c.rebalance_log || {};
  $('#core-rb').innerHTML = [['When', c.last_rebalance ? esc(fT(c.last_rebalance)) + ' UK' : '-'], ['Stocks picked', esc((L.picks || c.picks || []).join(', ') || '-')],
    ['Actions', esc((L.actions || []).join(' · ') || 'none')], ['Note', L.forced ? esc('Off-schedule re-pick: ' + L.forced) : 'Weekly ' + esc(R.rebalance)]].map(([k, v]) => `<div>${k}</div><div>${v}</div>`).join('');
}

/* ---------- day ---------- */
function renderDay() {
  const d = D.sleeves.day || {}, R = d.rules || {};
  $('#day-intro').innerHTML = `A <b>${fM0(d.start)}</b> pot for short-term trades based on price momentum and news sentiment. Up to <b>${R.max_open}</b> positions at a time, each ${R.alloc}% of the pot, ` +
    `with a <b>-${R.stop}%</b> stop-loss and <b>+${R.take}%</b> take-profit. New trades only ${esc(R.start)}-${esc(R.cutoff)} UK, and <b>everything is sold by ${esc(R.flatten)} UK</b> so nothing is held overnight. ` +
    `If the pot drops ${R.cap}% in a day, new trades stop until the next day.`;
  const st = D.risk.kill ? ['Paused', 'neg', 'kill switch on'] : d.halted ? ['Halted', 'neg', 'daily loss limit hit - no new trades today'] : d.flat_now ? ['Closed', '', `overnight - opens ${esc(R.start)} UK`] :
    d.entries_open ? ['Open', 'pos', `new trades until ${esc(R.cutoff)} UK`] : ['Entries closed', '', `after ${esc(R.cutoff)} UK - flat at ${esc(R.flatten)}`];
  const today = ymd.format(Date.now()), tr = (D.trades || []).filter(t => t.sleeve === 'day' && ymd.format(t.t * 1000) === today);
  const realT = tr.reduce((a, t) => a + (t.pnl || 0), 0);
  $('#day-stats').innerHTML = stat('Day pot value', fM(d.equity), '', `<span class="${cls(d.pnl)}">${fM(d.pnl, true)} (${fPct(d.ret)})</span> all time`) +
    stat("Today's P&L", `<span class="${cls(d.day_pnl)}">${fM(d.day_pnl, true)}</span>`, '', `${fPct(d.day_pnl_pct)} · limit -${R.cap}%`) +
    stat('Status', esc(st[0]), st[1], st[2]) + stat('Open positions', `${d.open_n} / ${R.max_open}`, '', `${fM(d.cash)} cash`) +
    stat('Win rate', isNum(d.win_rate) ? d.win_rate.toFixed(0) + '%' : '-', '', `${d.closed} closed trades`);
  const P = (D.positions || []).filter(p => p.sleeve === 'day');
  const cols = [{k: 'asset', l: 'Asset', asc: 1, f: r => assetCell(r.asset)}, {k: 'opened', l: 'Opened', f: r => esc(fTs(r.opened))},
    {k: 'entry', l: 'Buy price', num: 1, f: r => fP(r.entry)}, {k: 'now', l: 'Now', num: 1, f: r => fP(r.now)}, {k: 'value', l: 'Value', num: 1, f: r => fM(r.value)},
    {k: 'pnl', l: 'P&L', num: 1, f: r => pnlCell(r.pnl, r.pnl_pct)},
    {k: 'stop', l: 'Stop / target', num: 1, nosort: 1, f: r => `<span class="neg">${fP(r.stop)}</span> / <span class="pos">${fP(r.take)}</span>`}];
  table($('#day-pos'), cols, P, {k: 'opened', d: -1}, renderDay, {empty: d.flat_now ? 'No positions - the Day pot is flat overnight' : 'No open day positions', key: r => r.asset, onRow: openAsset});
  $('#day-open-sub').textContent = `${P.length} open · sold automatically at ${R.flatten} UK`;
  table($('#day-trades'), orderCols().filter(c => c.k !== 'sleeve'), tr, {k: 't', d: -1}, renderDay, {empty: 'No day trades yet today'});
  $('#day-tr-sub').innerHTML = `${tr.length} trade${tr.length === 1 ? '' : 's'} · realised <span class="${cls(realT)}">${fM(realT, true)}</span>`;
}

/* ---------- risk pot ---------- */
const fUsd = v => !isNum(v) ? '-' : v >= 1e9 ? '$' + (v / 1e9).toFixed(1) + 'B' : v >= 1e6 ? '$' + (v / 1e6).toFixed(0) + 'M' : '$' + Math.round(v).toLocaleString('en-GB');
function renderRiskPot() {
  const r = D.sleeves.risk; if (!r) return; const R = r.rules || {}, sc = r.screen || {};
  $('#risk-intro').innerHTML = `A <b>${fM0(r.start)}</b> pot (${r.pct}% of the account) for <b>high-risk</b> bets: small and mid-size crypto coins and niche stocks ` +
    `(quantum, space, nuclear, bitcoin miners). It buys on a <b>breakout</b> - a strong move up with heavy trading, near its recent high - checked against news sentiment. ` +
    `Each position is at most <b>${R.pos_pct}% of the pot (≤ £${R.max_pos})</b>, up to <b>${R.max_open}</b> at a time and ${R.per_day} new per day. ` +
    `Exits: <b>-${R.stop}%</b> stop-loss; once a position has been up <b>+${R.trail_after}%</b> it is sold if it falls <b>${R.trail}%</b> from its peak (locks in gains); anything still held after <b>${R.hold} days</b> is sold. ` +
    `If the pot falls <b>${R.cap}%</b>, new trades stop until reviewed. Only coins listed on CoinGecko with ≥ ${fUsd(R.min_mcap)} market cap and ≥ ${fUsd(R.min_vol)} daily volume (no stablecoins, wrapped or exchange tokens). Expect big swings.`;
  const st = D.risk.kill ? ['Paused', 'neg', 'kill switch on'] : r.halted ? ['Paused', 'neg', `down ${fPct(r.dd_pct)} - waiting for review`] : ['Active', 'pos', `${r.new_today || 0} of ${R.per_day} new trades today`];
  $('#risk-stats').innerHTML = stat('Risk pot value', fM(r.equity), '', `<span class="${cls(r.pnl)}">${fM(r.pnl, true)} (${fPct(r.ret)})</span> since start`) +
    stat('Cash', fM(r.cash), '', `${fM(r.invested)} invested`) + stat('Open positions', `${r.open_n} / ${R.max_open}`, '', `max £${R.max_pos} each`) +
    stat('Status', esc(st[0]), st[1], st[2]) + stat('Drawdown', `<span class="${cls(r.dd_pct)}">${fPct(r.dd_pct)}</span>`, '', `limit -${R.cap}% · win rate ${isNum(r.win_rate) ? r.win_rate.toFixed(0) + '%' : '-'} of ${r.closed}`);
  const P = (D.positions || []).filter(p => p.sleeve === 'risk');
  const cols = [{k: 'asset', l: 'Asset', asc: 1, f: x => assetCell(x.asset)}, {k: 'opened', l: 'Opened', f: x => esc(fTs(x.opened))},
    {k: 'entry', l: 'Avg buy', num: 1, f: x => fP(x.entry)}, {k: 'now', l: 'Now', num: 1, f: x => fP(x.now)}, {k: 'value', l: 'Value', num: 1, f: x => fM(x.value)},
    {k: 'pnl', l: 'P&L', num: 1, f: x => pnlCell(x.pnl, x.pnl_pct)},
    {k: 'stop', l: 'Stop / trailing', num: 1, nosort: 1, f: x => `<span class="neg">${fP(x.stop)}</span> / ${x.trail_on ? `<span class="pos">${fP(x.trail)}</span>` : `<span class="muted">from ${fP(x.trail_arm)}</span>`}`},
    {k: 'expires', l: 'Sold by', f: x => `${esc(fTs(x.expires))}<div class="muted small">${esc(until(x.expires))}</div>`}];
  table($('#risk-pos'), cols, P, {k: 'opened', d: -1}, renderRiskPot, {empty: 'No open risk positions - waiting for a breakout', key: x => x.asset, onRow: openAsset});
  $('#risk-open-sub').textContent = `${P.length} open · stop -${R.stop}% · trailing ${R.trail}% after +${R.trail_after}% · max ${R.hold} days`;
  const priced = new Set((D.assets || []).map(a => a.a));
  $('#risk-cand-sub').innerHTML = `Ranked by breakout score (-1 to +1, mixed with fresh news sentiment). Buys at ≥ ${R.entry} when it is a real breakout, the short-term trend agrees and no limit blocks it. Updated ${esc(ago(r.status_t))}.`;
  table($('#risk-cands'), [
    {k: 'a', l: 'Asset', asc: 1, f: x => assetCell(x.a, x.kind === 'stock' ? (themeOf(x.a) || 'Stock') : 'Crypto')},
    {k: 'comb', l: 'Score', f: x => scoreBar(x.comb)}, {k: 'mom', l: 'Short-term trend', num: 1, f: x => isNum(x.mom) ? `<span class="${cls(x.mom)}">${x.mom.toFixed(2)}</span>` : '<span class="muted">-</span>'},
    {k: 'why', l: 'Why', wrap: 1, f: x => esc(x.why.split(' | no: ')[0])},
    {k: 'block', l: 'Decision', wrap: 1, sv: x => x.block ? 1 : 0, f: x => x.block ? `<span class="muted small">${esc(x.block)}</span>` : '<span class="tag buy">Bought</span>'}],
    r.cands || [], S.rcSort, renderRiskPot, {empty: 'No candidates yet', key: x => x.a, onRow: a => priced.has(a) && openAsset(a), limit: 25});
  $('#risk-screen-sub').innerHTML = `Every 15 minutes the top ${R.top_n} coins by market cap are screened: ${sc.liquid ?? '-'} pass the size/volume/scam filters, ${sc.ok ?? '-'} look like a breakout right now` +
    `${sc.monad ? ` (${sc.monad} Monad-ecosystem tokens checked for KyberSwap liquidity)` : ''}. The best ones are tracked live (Markets tab). Updated ${esc(ago(sc.t))}.`;
  table($('#risk-screen'), [
    {k: 'a', l: 'Coin', asc: 1, f: x => assetCell(x.a, (x.name || '') + (x.monad ? ' · Monad' : ''))}, {k: 'rank', l: 'Rank', num: 1, asc: 1, f: x => '#' + x.rank},
    {k: 'mcap', l: 'Market cap', num: 1, f: x => fUsd(x.mcap)}, {k: 'vol', l: '24h volume', num: 1, f: x => fUsd(x.vol)},
    {k: 'c24', l: '24h', num: 1, f: x => `<span class="${cls(x.c24)}">${fPct(x.c24, 1)}</span>`}, {k: 'c7', l: '7d', num: 1, f: x => `<span class="${cls(x.c7)}">${fPct(x.c7, 1)}</span>`},
    {k: 'score', l: 'Breakout', f: x => scoreBar(x.score)},
    {k: 'ok', l: 'Status', sv: x => x.ok ? 0 : 1, asc: 1, f: x => x.ok ? '<span class="tag blue">Breakout</span>' : `<span class="muted small">${esc((x.why.split(' | no: ')[1] || ''))}</span>`}],
    sc.top || [], S.rsSort, renderRiskPot, {empty: 'Screen not run yet', key: x => x.a, onRow: a => priced.has(a) && openAsset(a), limit: 25});
  const tr = (D.trades || []).filter(t => t.sleeve === 'risk'), real = tr.reduce((a, t) => a + (t.pnl || 0), 0);
  table($('#risk-trades'), orderCols().filter(c => c.k !== 'sleeve'), tr, {k: 't', d: -1}, renderRiskPot, {empty: 'No risk pot orders yet', limit: 30});
  $('#risk-tr-sub').innerHTML = `${tr.filter(t => t.side !== 'TRANSFER').length} orders · realised <span class="${cls(real)}">${fM(real, true)}</span>`;
}

/* ---------- markets ---------- */
function renderMarkets() {
  const q = S.q.trim().toLowerCase();
  const rows = (D.assets || []).filter(a => (S.filter === 'all' || (S.filter === 'held' ? a.pos : a.k === S.filter)) && (!q || a.a.toLowerCase().includes(q) || (a.theme || '').toLowerCase().includes(q)));
  const cols = [
    {k: 'a', l: 'Asset', asc: 1, f: r => assetCell(r.a, r.k === 'stock' ? (r.theme || 'Stock') : 'Crypto')},
    {k: 'gbp', l: 'Price', num: 1, f: r => `${fP(r.gbp)}<div class="muted small">${fP(r.usd, '$')}</div>`},
    {k: 'chg', l: '24h', num: 1, f: r => `<span class="${cls(r.chg)}">${fPct(r.chg)}</span>`},
    {k: 'spark', l: 'Last 48h', nosort: 1, f: r => spark(r.spark)},
    {k: 'score', l: 'Signal', f: r => scoreBar(r.score, r.action === 'BUY' || r.action === 'SELL' ? sideTag(r.action) : '')},
    {k: 'sent', l: 'Sentiment', sv: r => r.sent ? r.sent.s : null, f: r => r.sent ? `<span class="${cls(r.sent.s)}">${r.sent.s >= 0 ? '+' : ''}${r.sent.s.toFixed(2)}</span> <span class="muted small">${esc(ago(r.sent.t))}</span>` : '<span class="muted">-</span>'},
    {k: 'pos', l: 'Held', sv: r => posVal(r.a), f: r => r.pos ? (r.held || []).map(potTag).join(' ') : '<span class="muted">-</span>'},
    {k: 'core_t', l: 'Core target', num: 1, f: r => isNum(r.core_t) ? r.core_t.toFixed(1) + '%' : '<span class="muted">-</span>'},
  ];
  table($('#asset-table'), cols, rows, S.aSort, renderMarkets, {key: r => r.a, onRow: openAsset, empty: 'No assets match'});
  const l = D.risk.limits;
  $('#mk-note').textContent = `Signal = the Day pot's momentum + sentiment score (-1 to +1): it buys at ≥ ${l.buy} (≥ ${l.focus_buy} for MON, WLD, XRP) and sells at ≤ ${l.sell}. Stocks are priced live for the Core picks, held stocks and the top-ranked watchlist names; the full watchlist is ranked daily on the Core pot tab.`;
}

/* ---------- sentiment ---------- */
function renderSentiment() {
  const latest = {}; (D.sentiment || []).forEach(s => { if (!latest[s.a]) latest[s.a] = s; });
  const rows = Object.values(latest).sort((a, b) => b.s - a.s), el = $('#sent-overview');
  el.innerHTML = rows.length ? '<div class="sov">' + rows.map(s => { const w = Math.min(50, Math.abs(s.s) * 50), col = s.s >= 0 ? C.green : C.red;
    const op = 0.35 + 0.65 * (s.c || 0), stale = Date.now() / 1000 - s.t > 12 * 3600;
    return `<div class="sov-row" data-a="${esc(s.a)}" title="${esc(fT(s.t))} · confidence ${s.c}"><b>${esc(s.a)}</b>` +
      `<div class="b"><span style="left:${s.s >= 0 ? 50 : 50 - w}%;width:${w}%;background:${col};opacity:${op}"></span></div>` +
      `<span class="${cls(s.s)}" style="text-align:right${stale ? ';opacity:.5' : ''}">${s.s >= 0 ? '+' : ''}${s.s.toFixed(2)}</span></div>`; }).join('') + '</div>'
    : '<div class="muted" style="padding:20px 0;text-align:center">No sentiment yet.</div>';
  $$('.sov-row', el).forEach(r => r.onclick = () => openAsset(r.dataset.a));
  const sel = $('#sent-asset'), have = Object.keys(latest).sort();
  if ([...sel.options].slice(1).map(o => o.value).join(',') !== have.join(',')) { sel.innerHTML = '<option value="">All assets</option>' + have.map(a => `<option>${esc(a)}</option>`).join(''); sel.value = S.sAsset; }
  const all = (D.sentiment || []).filter(s => !S.sAsset || s.a === S.sAsset), shown = all.slice(0, S.sLimit);
  $('#sent-list').innerHTML = shown.length ? shown.map(s => sentItem(s)).join('') +
    (all.length > S.sLimit ? `<div style="padding-top:12px"><button class="btn ghost" id="sent-more">Show more (${all.length - S.sLimit} older)</button></div>` : '') : '<div class="muted">No sentiment rows yet.</div>';
  const more = $('#sent-more'); if (more) more.onclick = () => { S.sLimit += 25; renderSentiment(); };
}
function sentItem(s, showAsset = true) {
  const links = (s.src || []).map(safeUrl).filter(Boolean).map((u, i) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">[${i + 1}] ${esc(host(u))}</a>`).join('');
  return `<div class="sent-item"><div class="h">${showAsset ? `<b>${esc(s.a)}</b>` : ''}<span class="${cls(s.s)}">score ${s.s >= 0 ? '+' : ''}${s.s.toFixed(2)}</span>` +
    `<span>confidence ${isNum(s.c) ? s.c.toFixed(2) : '-'}</span><span>${esc(s.v)} posts/articles</span><span>${esc(fT(s.t))}</span></div>` +
    `<p>${esc(s.sum || '(no summary)')}</p>${links ? `<div class="src">${links}</div>` : ''}</div>`;
}

/* ---------- orders ---------- */
function orderRows() {
  return (D.trades || []).filter(t => (!S.tAsset || t.asset === S.tAsset) && (!S.tSide || t.side === S.tSide) && (!S.tSleeve || t.sleeve === S.tSleeve) &&
    (!S.tFrom || ymd.format(t.t * 1000) >= S.tFrom) && (!S.tTo || ymd.format(t.t * 1000) <= S.tTo));
}
const orderCols = (withAsset = true) => [
  {k: 't', l: 'Time (UK)', f: r => esc(fTs(r.t))},
  {k: 'sleeve', l: 'Pot', asc: 1, f: r => potTag(r.sleeve)},
  {k: 'side', l: 'Side', asc: 1, f: r => sideTag(r.side)},
  ...(withAsset ? [{k: 'asset', l: 'Asset', asc: 1, f: r => assetLink(r.asset)}] : []),
  {k: 'qty', l: 'Quantity', num: 1, f: r => fQty(r.qty)},
  {k: 'fill', l: 'Price', num: 1, f: r => `${fP(r.fill)}<div class="muted small">${fP(r.usd, '$')} mkt</div>`},
  {k: 'value', l: 'Value', num: 1, f: r => fM(r.value)},
  {k: 'fee', l: 'Fee', num: 1, f: r => fFee(r.fee)},
  {k: 'gas', l: 'Gas', num: 1, f: r => isNum(r.gas) ? fGas(r.gas) : '<span class="muted">-</span>'},
  {k: 'pnl', l: 'P&L', num: 1, f: r => pnlCell(r.pnl)},
  {k: 'reason', l: 'Reason', asc: 1, wrap: 1, f: r => esc(r.reason)},
];
function renderOrders() {
  const sel = $('#tr-asset'), have = [...new Set((D.trades || []).map(t => t.asset))].sort();
  if ([...sel.options].slice(1).map(o => o.value).join(',') !== have.join(',')) { sel.innerHTML = '<option value="">All assets</option>' + have.map(a => `<option>${esc(a)}</option>`).join(''); sel.value = S.tAsset; }
  const rows = orderRows();
  table($('#trade-table'), orderCols(), rows, S.tSort, renderOrders, {empty: (D.trades || []).length ? 'No orders match the filters' : 'No orders yet', limit: S.tLimit});
  const pnl = rows.reduce((a, t) => a + (t.pnl || 0), 0), fees = rows.reduce((a, t) => a + (t.fee || 0), 0), gas = rows.reduce((a, t) => a + (t.gas || 0), 0);
  const closed = rows.filter(t => isNum(t.pnl)), wins = closed.filter(t => t.pnl > 0).length;
  $('#tr-sum').innerHTML = `${rows.length} order${rows.length === 1 ? '' : 's'} · realised P&L <span class="${cls(pnl)}">${fM(pnl, true)}</span> · fees ${fFee(fees)} · gas ${fGas(gas)}` +
    (closed.length ? ` · ${wins} of ${closed.length} sells in profit` : '') + ((D.trades || []).length >= 500 ? ' · latest 500 orders' : '') +
    (rows.length > S.tLimit ? ` · <button class="btn ghost" id="tr-more">Show ${Math.min(100, rows.length - S.tLimit)} more</button>` : '');
  const more = $('#tr-more'); if (more) more.onclick = () => { S.tLimit += 100; renderOrders(); };
}
function exportCsv() {
  const q = v => { const s = String(v ?? ''); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
  const head = ['time_uk', 'pot', 'side', 'asset', 'qty', 'price_gbp', 'market_usd', 'value_gbp', 'fee_gbp', 'gas_gbp', 'pnl_gbp', 'reason'];
  const lines = orderRows().map(t => [fTs(t.t), POT[t.sleeve] || t.sleeve, t.side, t.asset, t.qty, t.fill, t.usd, t.value, t.fee, t.gas, t.pnl, t.reason].map(q).join(','));
  const url = URL.createObjectURL(new Blob([head.join(',') + '\n' + lines.join('\n')], {type: 'text/csv'}));
  const a = document.createElement('a'); a.href = url; a.download = 'paper-orders.csv'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------- risk + feeds (overview > details) ---------- */
function renderRisk() {
  const r = D.risk, l = r.limits, g = D.gas || {};
  const kv = [['Kill switch', r.kill ? '<b class="neg">ON - all trading paused</b>' : 'off'],
    ['Day pot loss limit', r.halted ? `<b class="neg">HIT</b> - no new day trades for ${esc(r.day)}` : `ok · ${fPct((D.sleeves.day || {}).day_pnl_pct)} today (limit -${l.daily_cap}%)`],
    ['Day pot positions', `${(D.sleeves.day || {}).open_n ?? '-'} of max ${l.max_open} · ${l.max_pos_pct}% of the pot each`],
    ['Stops', `Day pot -${l.stop}% / +${l.take}% · Core pot -${l.core_stop}% from average cost · Risk pot -${l.risk_stop}% + trailing`],
    ['Risk pot drawdown limit', (D.sleeves.risk || {}).halted ? `<b class="neg">HIT</b> - no new risk trades until reviewed` : `ok · ${fPct((D.sleeves.risk || {}).dd_pct)} vs base (limit -${l.risk_cap}%)`],
    ['Costs per trade', `${l.fee}% fee + slippage, plus Monad gas ${fGas(g.gbp ?? g.fallback)} · min trade £${l.min_trade}`]];
  if (r.last_halt) kv.push(['Last halt', `${esc(fTs(r.last_halt.t))} - ${esc(r.last_halt.reason)}`]);
  $('#risk-box').innerHTML = kv.map(([k, v]) => `<div>${esc(k)}</div><div>${v}</div>`).join('');
  const f = D.fx, m = D.mon_check;
  const fv = [['GBP/USD', `${isNum(f.gbpusd) ? f.gbpusd.toFixed(4) : '-'} via ${esc(f.source || '-')} (${esc(ago(f.t))})${f.stale ? ' <b class="neg">stale</b>' : ''}${f.alt ? ` · check ${f.alt.toFixed(4)} via ${esc(f.alt_source)}` : ''}`],
    ['Monad gas', isNum(g.gbp) ? `${g.gwei} gwei × ${(g.units || 0).toLocaleString('en-GB')} gas = ${g.mon} MON ≈ ${fGas(g.gbp)} per swap (${esc(ago(g.t))})` : `not fetched - using fallback ${fGas(g.fallback)}`],
    ['MON price check', m ? `CoinGecko $${m.cg} vs KyberSwap $${m.dex} (<span class="${cls(m.diff)}">${fPct(m.diff)}</span>)` : '-'],
    ['Collector', esc(D.collector || '-')], ['Last trader run', esc(fT(D.updated))]];
  $('#feed-box').innerHTML = fv.map(([k, v]) => `<div>${esc(k)}</div><div>${v}</div>`).join('');
}

/* ---------- asset detail drawer ---------- */
function openAsset(a) { location.hash = 'asset=' + encodeURIComponent(a); }
function closeAsset() { location.hash = S.tab; }
function hideDrawer() { if (!S.open && $('#drawer').classList.contains('hidden')) return;
  S.open = null; $('#drawer').classList.add('hidden'); $('#drawer').setAttribute('aria-hidden', 'true'); document.body.style.overflow = '';
  ['d', 'dsig', 'dsent'].forEach(dropChart); }
function showDrawer(a) {
  if (!D) return; const A = assetBy(a);
  if (!A && !(D.prices || {})[a]) { hideDrawer(); return; }
  const first = S.open !== a; S.open = a;
  const dr = $('#drawer'); dr.classList.remove('hidden'); dr.setAttribute('aria-hidden', 'false'); document.body.style.overflow = 'hidden';
  if (first) $('.drawer-panel').scrollTop = 0;
  const p = posBy(a), se = sentLatest(a);
  $('#d-title').innerHTML = `${esc(a)} ${posAll(a).map(x => potTag(x.sleeve)).join(' ')}`;
  $('#d-sub').textContent = A ? `${A.k === 'stock' ? 'xStock (on-chain share)' + (A.theme ? ' · ' + A.theme : '') : 'Crypto'} · price from ${A.src} · ${ago(A.t)}` : '';
  const k = [['Price', fP(A && A.gbp), ''], ['Price in $', fP(A && A.usd, '$'), ''], ['24h', fPct(A && A.chg), cls(A && A.chg)],
    ['Signal', A && isNum(A.score) ? (A.score >= 0 ? '+' : '') + A.score.toFixed(2) : '-', cls(A && A.score)],
    ['Sentiment', se ? (se.s >= 0 ? '+' : '') + se.s.toFixed(2) : '-', cls(se && se.s)],
    ...(posAll(a).length ? posAll(a).map(x => [POT[x.sleeve] || x.sleeve, `${fM(x.value)} (${fPct(x.pnl_pct)})`, cls(x.pnl)]) : [['Holding', 'none', '']])];
  $('#d-stats').innerHTML = k.map(([l, v, c]) => stat(l, esc(v), c)).join('');
  seg('d-ccy', 'c', S.ccy); seg('d-type', 't', S.ptype); seg('d-range', 'r', S.dRange);
  renderPriceChart(a, p); renderSigChart(a, A); renderReasons(a); renderSentTimeline(a);
  table($('#d-trades'), orderCols(false), (D.trades || []).filter(t => t.asset === a), {k: 't', d: -1}, () => showDrawer(a), {empty: 'No orders in this asset'});
}
function renderPriceChart(a, p) {
  dropChart('d'); if (!LC) return;
  const raw = (D.prices || {})[a] || [], usd = S.ccy === 'usd', sym = usd ? '$' : '£';
  const bars = raw.map(([t, o, h, l, c]) => { const f = usd ? 1 : fxAt(t); return {time: t, open: o * f, high: h * f, low: l * f, close: c * f}; });
  emptyMsg('d-empty', bars.length ? '' : 'No price history for this asset yet.');
  if (!bars.length) return;
  const ch = charts.d = mkChart($('#d-chart')), last = bars[bars.length - 1].close;
  const pf = pfmt(last); const fmt = {type: 'custom', minMove: pf.minMove, formatter: v => sym + v.toFixed(pf.precision)};
  const s = S.ptype === 'line' ? ch.addAreaSeries({lineColor: C.blue, topColor: 'rgba(29,155,240,.22)', bottomColor: 'rgba(29,155,240,0)', lineWidth: 2, priceFormat: fmt})
    : ch.addCandlestickSeries({upColor: C.green, downColor: C.red, wickUpColor: C.green, wickDownColor: C.red, borderVisible: false, priceFormat: fmt});
  s.setData(S.ptype === 'line' ? bars.map(b => ({time: b.time, value: b.close})) : bars);
  const times = bars.map(b => b.time);
  const snap = t => { const h = t - t % 3600; let best = times[0]; for (const x of times) { if (Math.abs(x - h) < Math.abs(best - h)) best = x; } return best; };
  const t0 = times[0] - 3600, t1 = times[times.length - 1] + 3600;
  s.setMarkers((D.trades || []).filter(t => t.asset === a && t.t >= t0 && t.t <= t1).map(t => ({time: snap(t.t), position: t.side === 'BUY' ? 'belowBar' : 'aboveBar',
    color: t.side === 'BUY' ? C.green : C.red, shape: t.side === 'BUY' ? 'arrowUp' : 'arrowDown',
    text: t.side === 'BUY' ? `B ${fM(t.value)}` : `S ${isNum(t.pnl) ? fM(t.pnl, true) : ''}`})).sort((x, y) => x.time - y.time));
  if (p) { const f = usd ? (p.now_usd && p.now ? p.now_usd / p.now : 1) : 1, tg = p.sleeve;
    if (isNum(p.trail)) s.createPriceLine({price: p.trail * f, color: C.green, lineStyle: LC.LineStyle.Dotted, lineWidth: 1, axisLabelVisible: true, title: 'trailing stop'});
    s.createPriceLine({price: p.entry * f, color: C.blue, lineStyle: LC.LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: tg + ' avg buy'});
    if (isNum(p.stop)) s.createPriceLine({price: p.stop * f, color: C.red, lineStyle: LC.LineStyle.Dotted, lineWidth: 1, axisLabelVisible: true, title: tg + ' stop'});
    if (isNum(p.take)) s.createPriceLine({price: p.take * f, color: C.green, lineStyle: LC.LineStyle.Dotted, lineWidth: 1, axisLabelVisible: true, title: 'target'}); }
  setRange(ch, times, S.dRange);
  $('#d-chart').ondblclick = () => { S.dRange = 'ALL'; seg('d-range', 'r', 'ALL'); ch.timeScale().fitContent(); };
}
function renderSigChart(a, A) {
  dropChart('dsig'); if (!LC) return;
  const h = ((D.signals || {})[a] || []).filter(x => isNum(x[1]));
  emptyMsg('d-sig-empty', h.length ? '' : 'No signal history yet.');
  if (!h.length) return;
  const ch = charts.dsig = mkChart($('#d-sig'), {rightPriceScale: {borderVisible: false, scaleMargins: {top: .1, bottom: .1}}});
  const s = ch.addLineSeries({color: C.blue, lineWidth: 2, pointMarkersVisible: h.length < 30, priceFormat: {type: 'price', precision: 2, minMove: 0.01}});
  s.setData(h.map(x => ({time: x[0], value: x[1]})));
  const buy = A && A.focus ? D.risk.limits.focus_buy : D.risk.limits.buy;
  s.createPriceLine({price: buy, color: C.green, lineStyle: LC.LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: 'buy'});
  s.createPriceLine({price: D.risk.limits.sell, color: C.red, lineStyle: LC.LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: 'sell'});
  s.setMarkers(h.filter(x => x[2] === 'BUY' || x[2] === 'SELL').map(x => ({time: x[0], position: x[2] === 'BUY' ? 'belowBar' : 'aboveBar',
    color: x[2] === 'BUY' ? C.green : C.red, shape: 'circle', text: x[2] === 'BUY' ? 'Buy' : 'Sell'})));
  ch.timeScale().fitContent();
}
function renderReasons(a) {
  const r = (D.reasons || {})[a] || [];
  $('#d-reasons').innerHTML = r.length ? r.map(x => `<div class="reason"><div class="h">${potTag(x.sleeve)}${sideTag(x.action)}<span>${esc(fT(x.t))}</span>` +
    `${isNum(x.score) ? `<span>score ${x.score.toFixed(2)}</span>` : ''}${isNum(x.mom) ? `<span>momentum ${x.mom.toFixed(2)}</span>` : ''}${isNum(x.sent) ? `<span>sentiment ${x.sent.toFixed(2)}</span>` : ''}</div>${esc(x.reason)}</div>`).join('')
    : '<div class="muted">No signals recorded for this asset in the last 7 days.</div>';
}
function renderSentTimeline(a) {
  dropChart('dsent');
  const rows = (D.sentiment || []).filter(s => s.a === a);
  $('#d-sent-list').innerHTML = rows.length ? rows.slice(0, 20).map(s => sentItem(s, false)).join('') : '';
  emptyMsg('d-sent-empty', rows.length ? '' : 'No sentiment for this asset yet.');
  if (!rows.length || !LC) return;
  const ch = charts.dsent = mkChart($('#d-sent'));
  const s = ch.addLineSeries({color: C.blue, lineWidth: 2, pointMarkersVisible: true, priceFormat: {type: 'price', precision: 2, minMove: 0.01}});
  const seen = new Map(); rows.slice().reverse().forEach(r => seen.set(r.t, r.s));
  s.setData([...seen.entries()].sort((x, y) => x[0] - y[0]).map(([t, v]) => ({time: t, value: v})));
  s.createPriceLine({price: 0, color: '#536471', lineStyle: LC.LineStyle.Dotted, lineWidth: 1, axisLabelVisible: false});
  ch.timeScale().fitContent();
}

/* ---------- tabs / routing ---------- */
const RENDER = {overview: () => { renderOverview(); renderEquity(); }, portfolio: renderPortfolio, core: renderCore, day: renderDay, risk: renderRiskPot,
  markets: renderMarkets, sentiment: renderSentiment, orders: renderOrders};
const safe = (name, fn) => { try { fn(); } catch (e) { console.error('render ' + name + ' failed:', e); } };
function showTab(t) {
  if (!TABS.includes(t)) t = 'overview';
  S.tab = t;
  $$('.tab').forEach(s => s.classList.toggle('on', s.dataset.tab === t));
  $$('#tabs a').forEach(a => { const on = a.dataset.tab === t; a.classList.toggle('on', on); a.setAttribute('aria-selected', on ? 'true' : 'false'); });
  const cur = $(`#tabs a[data-tab="${t}"]`); if (cur && cur.scrollIntoView) cur.scrollIntoView({block: 'nearest', inline: 'nearest'});
  document.title = `${$(`#tabs a[data-tab="${t}"]`).textContent} - Paper Trader`;
  if (D) safe(t, RENDER[t]);
}
function route() {
  const h = decodeURIComponent(location.hash.replace(/^#/, ''));
  const m = h.match(/^asset=(.+)$/);
  if (m) { if (!$('.tab.on')) showTab(TABS.includes(S.tab) && S.tab !== 'overview' ? S.tab : 'markets'); if (D) showDrawer(m[1]); return; }
  hideDrawer();
  const t = TABS.includes(h) ? h : 'overview';
  if (t !== S.tab || !$('.tab.on')) { showTab(t); if (t !== 'overview' || h) window.scrollTo(0, 0); }
}
function renderAll() {
  safe('header', renderHeader);
  TABS.forEach(t => safe(t, t === 'overview' ? renderOverview : RENDER[t]));   // tables/text for every tab
  if (S.tab === 'overview') safe('equity', renderEquity);                        // chart only when visible
  if (S.open) showDrawer(S.open); else route();
  document.body.dataset.ready = '1';
}
async function load(force) {
  try {
    const r = await fetch('data.json?_=' + Date.now(), {cache: 'no-store'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json(); loadErr = null;
    if (force || !D || d.generated !== lastGen) { D = d; lastGen = d.generated; renderAll(); } else renderHeader();
  } catch (e) { loadErr = e.message || String(e); console.warn('data.json load failed:', loadErr); renderHeader(); }
}
function wire() {
  $('#btn-refresh').onclick = () => load(true);
  $$('#eq-range button').forEach(b => b.onclick = () => { S.eqRange = b.dataset.r; seg('eq-range', 'r', S.eqRange); if (D && charts.eq) setRange(charts.eq, D.equity.t, S.eqRange); });
  $$('#pf-pot button').forEach(b => b.onclick = () => { S.pPot = b.dataset.s; seg('pf-pot', 's', S.pPot); renderPortfolio(); });
  $('#rank-theme').onchange = e => { S.rTheme = e.target.value; renderCore(); };
  $$('#asset-filter button').forEach(b => b.onclick = () => { S.filter = b.dataset.f; seg('asset-filter', 'f', S.filter); renderMarkets(); });
  $('#asset-q').oninput = e => { S.q = e.target.value; renderMarkets(); };
  $('#sent-asset').onchange = e => { S.sAsset = e.target.value; S.sLimit = 15; renderSentiment(); };
  const ord = () => { S.tLimit = 50; renderOrders(); };
  $('#tr-sleeve').onchange = e => { S.tSleeve = e.target.value; ord(); };
  $('#tr-asset').onchange = e => { S.tAsset = e.target.value; ord(); };
  $('#tr-side').onchange = e => { S.tSide = e.target.value; ord(); };
  $('#tr-from').onchange = e => { S.tFrom = e.target.value; ord(); };
  $('#tr-to').onchange = e => { S.tTo = e.target.value; ord(); };
  $('#tr-clear').onclick = () => { S.tAsset = S.tSide = S.tSleeve = S.tFrom = S.tTo = ''; ['#tr-asset', '#tr-side', '#tr-sleeve', '#tr-from', '#tr-to'].forEach(s => $(s).value = ''); ord(); };
  $('#tr-csv').onclick = exportCsv;
  $$('[data-close]').forEach(b => b.onclick = closeAsset);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && S.open) closeAsset(); });
  $$('#d-ccy button').forEach(b => b.onclick = () => { S.ccy = b.dataset.c; seg('d-ccy', 'c', S.ccy); renderPriceChart(S.open, posBy(S.open)); });
  $$('#d-type button').forEach(b => b.onclick = () => { S.ptype = b.dataset.t; seg('d-type', 't', S.ptype); renderPriceChart(S.open, posBy(S.open)); });
  $$('#d-range button').forEach(b => b.onclick = () => { S.dRange = b.dataset.r; seg('d-range', 'r', S.dRange);
    setRange(charts.d, ((D.prices || {})[S.open] || []).map(x => x[0]), S.dRange); });
  window.addEventListener('hashchange', route);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(false); });
  setInterval(() => load(false), REFRESH_MS);
  setInterval(() => { if (D) renderHeader(); }, 15000);
}

if (!LC) { const al = document.getElementById('alert'); al.textContent = 'Charting library failed to load (CDN blocked?). Tables still work.'; al.classList.remove('hidden'); }
wire(); route(); load(true);
})();

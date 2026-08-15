/* THE TICKER — client.
 *
 * Three states driven by one fact: signed in? paid? → gate, subscribe, or paper.
 * Token in localStorage so the installed PWA survives a cold start; the native
 * app will use the same bearer-token API unchanged.
 */
'use strict';

const $ = (id) => document.getElementById(id);

const token = () => localStorage.getItem('ticker_token');
const setToken = (t) => t ? localStorage.setItem('ticker_token', t)
                          : localStorage.removeItem('ticker_token');

const api = (path, opts = {}) => {
  const headers = { ...(opts.headers || {}) };
  if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const t = token();
  if (t) headers['Authorization'] = `Bearer ${t}`;
  return fetch(path, { ...opts, headers });
};

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const LONG_DATE = { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' };

let me = null, es = null, soundOn = false, audioCtx = null;
const seen = new Set();

// ───────────────────────── screens ─────────────────────────

function show(which) {
  $('authScreen').hidden = which !== 'auth';
  $('paywallScreen').hidden = which !== 'paywall';
  $('appScreen').hidden = which !== 'app';
}

async function boot() {
  const today = new Date().toLocaleDateString(undefined, LONG_DATE).toUpperCase();
  $('authDate').textContent = today;
  $('todayDate').textContent = today;
  loadBillingConfig();

  if (!token()) return show('auth');
  const r = await api('/api/auth/me');
  if (!r.ok) { setToken(null); return show('auth'); }
  me = await r.json();
  me.entitled ? enterApp() : show('paywall');
}

async function loadBillingConfig() {
  try {
    const r = await api('/api/billing/config');
    // Must check r.ok: a 401/500 still parses as JSON, and treating that body
    // as config made `cfg.configured` undefined — which wrongly revealed the
    // dev-grant button and showed an undefined price.
    if (!r.ok) return;
    const cfg = await r.json();
    if (cfg.price_display) {
      $('priceHint').textContent = cfg.price_display;
      $('priceBig').textContent = cfg.price_display;
    }
    $('devGrantBtn').hidden = cfg.configured !== false;
  } catch { /* cosmetic only — never block boot */ }
}

// ───────────────────────── auth ─────────────────────────

let mode = 'login';
document.querySelectorAll('.tab').forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    mode = tab.dataset.mode;
    $('authSubmit').textContent = mode === 'login' ? 'Sign In' : 'Register';
    $('password').autocomplete = mode === 'login' ? 'current-password' : 'new-password';
    $('authError').hidden = true;
  };
});

$('authForm').onsubmit = async (e) => {
  e.preventDefault();
  const btn = $('authSubmit');
  btn.disabled = true;
  $('authError').hidden = true;
  try {
    const r = await api(`/api/auth/${mode}`, {
      method: 'POST',
      body: JSON.stringify({ email: $('email').value, password: $('password').value }),
    });
    const data = await r.json();
    if (!r.ok) {
      // FastAPI validation errors arrive as a list of objects.
      const d = data.detail;
      throw new Error(Array.isArray(d) ? (d[0]?.msg || 'Invalid input')
                                       : (d || 'Something went wrong'));
    }
    setToken(data.access_token);
    me = await (await api('/api/auth/me')).json();
    me.entitled ? enterApp() : show('paywall');
  } catch (err) {
    $('authError').textContent = err.message;
    $('authError').hidden = false;
  } finally {
    btn.disabled = false;
  }
};

const signOut = () => { setToken(null); if (es) es.close(); location.href = '/'; };
$('logoutBtn').onclick = signOut;
$('logoutFromPaywall').onclick = signOut;

// ───────────────────────── purchase ─────────────────────────

$('buyBtn').onclick = async () => {
  const btn = $('buyBtn');
  btn.disabled = true; btn.textContent = 'Opening Checkout…';
  try {
    const r = await api('/api/billing/checkout', { method: 'POST' });
    const data = await r.json();
    if (!r.ok) {
      const d = data.detail;
      throw new Error(Array.isArray(d) ? (d[0]?.msg || 'Checkout unavailable')
                                       : (d || `Checkout failed (${r.status})`));
    }
    if (data.already_entitled) return boot();
    window.location.href = data.url;
  } catch (err) {
    $('buyError').textContent = err.message;
    $('buyError').hidden = false;
    btn.disabled = false; btn.textContent = 'Purchase Access';
  }
};

$('devGrantBtn').onclick = async () => {
  const r = await api('/api/auth/dev-grant', { method: 'POST' });
  if (r.ok) { me = await r.json(); enterApp(); }
};

// Returning from Stripe: entitlement is granted by the webhook, which can land
// a beat after the redirect — so poll briefly instead of showing a stale paywall.
if (new URLSearchParams(location.search).get('paid') === '1') {
  let tries = 0;
  const poll = setInterval(async () => {
    if (++tries > 12) return clearInterval(poll);
    const r = await api('/api/auth/me');
    if (!r.ok) return;
    const u = await r.json();
    if (u.entitled) {
      clearInterval(poll);
      me = u;
      history.replaceState({}, '', '/');
      enterApp();
    }
  }, 1200);
}

// ───────────────────────── the paper ─────────────────────────

function enterApp() {
  show('app');
  loadAlerts();
  connectStream();
  loadWire();
  loadChart(7);
  initPush();
  setInterval(loadWire, 300000);
}

function stamp(text, cls) {
  const el = $('connPill');
  el.textContent = text;
  el.className = 'stamp' + (cls ? ' ' + cls : '');
}

async function loadAlerts() {
  try {
    const { events } = await (await api('/api/events?limit=40')).json();
    $('watchCount').textContent = `${events.length} dispatch${events.length === 1 ? '' : 'es'} on file`;
    if (!events.length) return;
    events.slice().reverse().forEach((e) => renderDispatch(e, false));
    updateBoard(events[0]);
  } catch { /* empty on first run is normal */ }
}

function connectStream() {
  if (es) es.close();
  es = new EventSource(`/api/events/stream?token=${encodeURIComponent(token())}`);
  es.onopen = () => stamp('on watch', 'live');
  es.onerror = () => stamp('reconnecting', 'warn');
  es.onmessage = (ev) => {
    let a; try { a = JSON.parse(ev.data); } catch { return; }
    if (a.type !== 'alert') return;
    if (seen.has(`${a.event_id}:${a.state}`)) return;
    renderDispatch(a, true);
    updateBoard(a);
    if (a.state === 'confirmed') chime(5);
    else if (a.state === 'likely') chime(2);
  };
}

function updateBoard(a) {
  const board = $('board');
  if (a.state === 'confirmed') {
    board.classList.add('alerting');
    $('boardState').textContent = 'Confirmed';
    $('boardDetail').textContent = a.headline;
  } else if (a.state === 'retracted') {
    board.classList.remove('alerting');
    $('boardState').textContent = 'Retracted';
    $('boardDetail').textContent = a.headline;
  } else {
    $('boardState').textContent = a.state === 'likely' ? 'Developing' : 'All Quiet';
    $('boardDetail').textContent = a.headline || 'No qualifying event detected.';
  }
  const when = new Date().toLocaleTimeString();
  $('boardSince').textContent = `Last dispatch ${when}`;
  $('footStatus').textContent = `Last dispatch ${when}`;
}

function renderDispatch(a, isNew) {
  seen.add(`${a.event_id}:${a.state}`);
  const list = $('alertList');
  list.querySelector('.empty')?.remove();

  const when = a.received_at ? new Date(a.received_at) : new Date();
  const lat = a.detect_latency_ms != null
    ? `${(a.detect_latency_ms / 1000).toFixed(2)}s` : '—';
  const ev = (a.payload?.evidence || []).slice(0, 3);

  const el = document.createElement('article');
  el.className = 'dispatch' + (isNew ? ' new' : '');
  el.innerHTML = `
    <div class="d-top">
      <span class="badge ${esc(a.state)}">${esc(a.state)}</span>
      <span>${when.toLocaleString()}</span>
      <span>&middot; detected in ${lat}</span>
      ${a.score != null ? `<span>&middot; score ${Number(a.score).toFixed(3)}</span>` : ''}
    </div>
    <div class="d-head">${esc(a.headline)}</div>
    <div class="d-meta">
      ${esc(a.target)}
      ${a.url ? ` &middot; <a href="${encodeURI(a.url)}" target="_blank" rel="noopener">read the source</a>` : ''}
      ${ev.length ? '<br>' + ev.map((e) =>
          `corroboration: tier ${e.tier} — ${esc(e.source)}`).join('<br>') : ''}
    </div>`;
  list.prepend(el);
  while (list.children.length > 60) list.lastChild.remove();
}

// ───────────────────────── sound ─────────────────────────

$('soundBtn').onclick = () => {
  soundOn = !soundOn;
  $('soundBtn').textContent = soundOn ? 'Sound On' : 'Sound Off';
  if (soundOn) chime(1);   // unlock AudioContext on this user gesture
};

function chime(times = 3) {
  if (!soundOn) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    for (let i = 0; i < times; i++) {
      const t0 = audioCtx.currentTime + i * 0.4;
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'square';
      osc.frequency.setValueAtTime(880, t0);
      osc.frequency.setValueAtTime(1180, t0 + 0.15);
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.25, t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.34);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(t0); osc.stop(t0 + 0.36);
    }
  } catch { /* autoplay policy — the visual dispatch still lands */ }
}

// ───────────────────────── push ─────────────────────────

function b64ToU8(b64) {
  const pad = '='.repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function initPush() {
  const btn = $('pushBtn');
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    btn.textContent = 'No Push'; btn.disabled = true; return;
  }
  let reg;
  try { reg = await navigator.serviceWorker.register('/sw.js'); }
  catch { btn.textContent = 'No Push'; btn.disabled = true; return; }

  if (await reg.pushManager.getSubscription()) {
    btn.textContent = 'Alerts On'; btn.disabled = true; return;
  }

  btn.onclick = async () => {
    btn.disabled = true; btn.textContent = 'Enabling…';
    try {
      if (await Notification.requestPermission() !== 'granted')
        throw new Error('Notification permission denied.');
      const { publicKey } = await (await api('/api/push/public-key')).json();
      if (!publicKey) throw new Error('Push is not configured on the server yet.');
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true, applicationServerKey: b64ToU8(publicKey),
      });
      const j = sub.toJSON();
      const r = await api('/api/push/register', {
        method: 'POST',
        body: JSON.stringify({ kind: 'webpush', token: j.endpoint, keys: j.keys,
                               user_agent: navigator.userAgent }),
      });
      if (!r.ok) throw new Error('Could not register this device.');
      btn.textContent = 'Alerts On';
    } catch (err) {
      btn.textContent = 'Alerts'; btn.disabled = false;
      alert(err.message);
    }
  };
}

$('testPushBtn').onclick = async () => {
  const d = await (await api('/api/push/test', { method: 'POST' })).json();
  alert(d.ok ? `Test dispatch sent to ${d.sent} device(s).`
             : `Nothing sent: ${d.error || (d.errors || []).join('; ') || 'unknown'}`);
};

// ───────────────────────── the wire ─────────────────────────

async function loadWire() {
  try {
    const { items } = await (await api('/api/news?limit=14')).json();
    const list = $('newsList');
    if (!items?.length) {
      list.innerHTML = '<div class="empty">The wire is quiet.</div>'; return;
    }
    list.innerHTML = items.map((i) => `
      <a class="wire-item" href="${encodeURI(i.url)}" target="_blank" rel="noopener">
        <div class="w-title">${esc(i.title)}</div>
        <div class="w-src">${esc(i.source || 'wire')}</div>
      </a>`).join('');
  } catch {
    $('newsList').innerHTML = '<div class="empty">The wire is unreachable.</div>';
  }
}

// ───────────────────────── market ─────────────────────────

let chartData = [];
const INK = '#16130f', RED = '#b22234', GREEN = '#17632f';

document.querySelectorAll('.chip').forEach((chip) => {
  chip.onclick = () => {
    document.querySelectorAll('.chip').forEach((c) => c.classList.remove('active'));
    chip.classList.add('active');
    loadChart(Number(chip.dataset.days));
  };
});

async function loadChart(days) {
  try {
    const d = await (await api(`/api/market/TRUMP?days=${days}`)).json();
    chartData = d.series || [];
    $('chartLast').textContent = d.last ? `$${Number(d.last).toFixed(4)}` : '—';
    const chg = Number(d.change_pct || 0);
    const el = $('chartChange');
    el.textContent = `${chg >= 0 ? '▲' : '▼'} ${Math.abs(chg).toFixed(2)}%`;
    el.className = 'q-chg ' + (chg >= 0 ? 'up' : 'down');
    // Say plainly when the series is invented. A chart passing synthetic
    // numbers off as market data would be worse than no chart.
    $('chartSource').textContent = d.source === 'synthetic'
      ? 'Demo data — live quote unavailable'
      : `Source: ${d.source}`;
    drawChart();
  } catch {
    $('chartSource').textContent = 'Quote unavailable';
  }
}

function drawChart() {
  const svg = $('chart');
  if (!chartData.length) { svg.innerHTML = ''; return; }
  const W = 600, H = 220, pad = 8;
  const vals = chartData.map((p) => p.c);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = (max - min) || 1;
  const x = (i) => (i / (chartData.length - 1 || 1)) * W;
  const y = (v) => H - pad - ((v - min) / span) * (H - pad * 2);

  const line = chartData.map((p, i) =>
    `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.c).toFixed(1)}`).join('');
  const up = vals[vals.length - 1] >= vals[0];
  const stroke = up ? GREEN : RED;

  // Hatched fill rather than a gradient — reads as engraving, not a dashboard.
  svg.innerHTML = `
    <defs>
      <pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse"
               patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="6" stroke="${stroke}" stroke-width="1.6"
              opacity=".22"/>
      </pattern>
    </defs>
    <line x1="0" y1="${H - pad}" x2="${W}" y2="${H - pad}" stroke="${INK}"
          stroke-width="1" opacity=".35"/>
    <path d="${line}L${W},${H - pad}L0,${H - pad}Z" fill="url(#hatch)"/>
    <path d="${line}" fill="none" stroke="${stroke}" stroke-width="2.4"
          vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
    <circle id="chartDot" r="4" fill="${stroke}" opacity="0"/>`;

  svg.onmousemove = (ev) => {
    const rect = svg.getBoundingClientRect();
    const i = Math.round(((ev.clientX - rect.left) / rect.width) * (chartData.length - 1));
    const p = chartData[Math.max(0, Math.min(chartData.length - 1, i))];
    if (!p) return;
    const dot = svg.querySelector('#chartDot');
    dot.setAttribute('cx', x(i)); dot.setAttribute('cy', y(p.c));
    dot.setAttribute('opacity', '1');
    const tip = $('chartTip');
    tip.hidden = false;
    tip.textContent = `$${p.c.toFixed(4)} — ${new Date(p.t).toLocaleDateString()}`;
    tip.style.left = `${Math.min(ev.clientX - rect.left, rect.width - 150)}px`;
    tip.style.top = `${Math.max(0, ev.clientY - rect.top - 32)}px`;
  };
  svg.onmouseleave = () => {
    $('chartTip').hidden = true;
    svg.querySelector('#chartDot')?.setAttribute('opacity', '0');
  };
}

boot();

/* TRUMP DEATH WATCHER — client.
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

let me = null, es = null, soundOn = false, audioCtx = null, clockTimer = null;
const seen = new Set();

// ───────────────────────── screens ─────────────────────────

function show(which) {
  $('authScreen').hidden = which !== 'auth';
  $('paywallScreen').hidden = which !== 'paywall';
  $('appScreen').hidden = which !== 'app';
}

// Signed out is a valid way to read this site. The paper is public; only
// delivery — push alerts, the live wire, the chime — is bought.
const entitled = () => !!(me && me.entitled);

async function boot() {
  const today = new Date().toLocaleDateString(undefined, LONG_DATE).toUpperCase();
  $('authDate').textContent = today;
  $('todayDate').textContent = today;
  loadBillingConfig();

  if (token()) {
    const r = await api('/api/auth/me');
    if (r.ok) me = await r.json();
    else setToken(null);          // expired or revoked: browse on as a visitor
  }
  enterApp();
}

/* Every entitlement-dependent control in one place. Called on boot and after
   any sign-in, sign-out or purchase, so the chrome can never disagree with the
   token we are actually holding. The server enforces all of this regardless —
   see routers/events.py; this only decides what is worth offering. */
function applyAccess() {
  const paid = entitled();
  $('pushBtn').hidden = !paid;
  $('soundBtn').hidden = !paid;
  $('testPushBtn').hidden = !paid;
  $('unlockBtn').hidden = paid;
  $('lockNotice').hidden = paid;
  $('signInBtn').hidden = !!me;
  $('logoutBtn').hidden = !me;
  $('unlockBtn').textContent = me ? 'Unlock Alerts' : 'Get Alerts';
  if (!paid) stamp('public view');
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
      $('lockPrice').textContent = cfg.price_display;
      $('ledePrice').textContent = cfg.price_display;
    }
    // null means "not enough sales to be worth saying" — the server decides,
    // so the client has no threshold policy of its own to drift out of step.
    const sold = cfg.purchase_count;
    const line = sold ? `${sold.toLocaleString()} people have unlocked alerts` : '';
    for (const id of ['buyerCount', 'buyerCountPaywall']) {
      $(id).textContent = line;
      $(id).hidden = !line;
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
    // Refresh the chrome even when we land on the paywall — otherwise going
    // back to the paper would still show the signed-out buttons.
    applyAccess();
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

// The gates are destinations now, not a wall you wake up behind, so every one
// of them needs a way back to the paper.
$('signInBtn').onclick = () => show('auth');
$('backFromAuth').onclick = () => show('app');
$('backFromPaywall').onclick = () => show('app');
const goUnlock = () => show(me ? 'paywall' : 'auth');
$('unlockBtn').onclick = goUnlock;
$('unlockBtn2').onclick = goUnlock;

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

// Returning from Stripe. Entitlement is normally granted by the webhook, but
// we also ask the server to verify the session directly with Stripe — so a
// webhook that is misconfigured or briefly down cannot leave you having paid
// with nothing to show for it.
(() => {
  const qs = new URLSearchParams(location.search);
  if (qs.get('paid') !== '1') return;
  const sessionId = qs.get('session_id');

  const finish = (u) => {
    me = u;
    history.replaceState({}, '', '/');
    enterApp();
  };

  (async () => {
    // 1. Ask the server to verify with Stripe (authoritative, not browser-trusted).
    if (sessionId) {
      try {
        const r = await api('/api/billing/confirm', {
          method: 'POST', body: JSON.stringify({ session_id: sessionId }),
        });
        if (r.ok && (await r.json()).entitled) {
          const u = await (await api('/api/auth/me')).json();
          if (u.entitled) return finish(u);
        }
      } catch { /* fall through to polling */ }
    }
    // 2. Otherwise wait for the webhook to land.
    let tries = 0;
    const poll = setInterval(async () => {
      if (++tries > 12) {
        clearInterval(poll);
        const err = $('buyError');
        if (err) {
          err.textContent = 'Payment received, but access has not activated yet. '
            + 'Reload in a moment, or contact support with your receipt.';
          err.hidden = false;
        }
        return;
      }
      const r = await api('/api/auth/me');
      if (!r.ok) return;
      const u = await r.json();
      if (u.entitled) { clearInterval(poll); finish(u); }
    }, 1500);
  })();
})();

// ───────────────────────── the paper ─────────────────────────

function enterApp() {
  show('app');
  applyAccess();
  loadAlerts();
  loadWire();
  loadChart(7);
  loadMemecoin('24h');
  // Both of these are the paid product. Calling them while signed out would
  // just collect 401/402s and light up the console for a visitor who is doing
  // nothing wrong.
  if (entitled()) { connectStream(); initPush(); }
  setInterval(loadWire, 300000);
  // Cleared first: enterApp() also runs on the return-from-Stripe path, and a
  // second timer would tick the clock twice a second.
  tickClock();
  clearInterval(clockTimer);
  clockTimer = setInterval(tickClock, 1000);
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
    updateVitals(events[0]);
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
    updateVitals(a);
    if (a.state === 'confirmed') chime(5);
    else if (a.state === 'likely') chime(2);
  };
}

// Detector state → the verdict on the board. `retracted` deliberately returns
// to ALIVE: a report that collapsed is the entire reason the retraction
// pipeline exists, and leaving the board reading DEAD would make it a liar.
const VERDICT = {
  confirmed: { status: 'dead', word: 'Dead',
               fallback: 'Confirmed across independent sources.' },
  likely:    { status: 'developing', word: 'Unconfirmed',
               fallback: 'Reports are circulating and have not cleared corroboration.' },
  retracted: { status: 'alive', word: 'Alive',
               fallback: 'The earlier report collapsed and has been retracted.' },
};
const VERDICT_QUIET = {
  status: 'alive', word: 'Alive',
  fallback: 'No qualifying event detected. You will be told the instant one is.',
};

function updateVitals(a) {
  const v = VERDICT[a?.state] || VERDICT_QUIET;
  // One attribute drives the spine, dot, verdict colour and the dead-state
  // inversion — see .vitals[data-status] in styles.css.
  $('vitals').dataset.status = v.status;
  $('vitalVerdict').textContent = v.word;
  $('vitalDetail').textContent = a?.headline || v.fallback;

  const n = (a?.payload?.evidence || []).length;
  $('vitalConf').textContent = n
    ? `Corroboration — ${n} source${n === 1 ? '' : 's'}`
    : 'Awaiting corroboration';

  const when = new Date().toLocaleTimeString();
  $('vitalSince').textContent = `Last dispatch ${when}`;
  $('footStatus').textContent = `Last dispatch ${when}`;
}

function tickClock() {
  $('vitalClock').textContent = new Date().toLocaleTimeString([], { hour12: false });
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

// ───────────────────────── charts ─────────────────────────

const INK = '#16130f', RED = '#b22234', GREEN = '#17632f';
const SUB = '₀₁₂₃₄₅₆₇₈₉';

/* A memecoin trades around 0.0000003. toFixed(4) renders that as "0.0000",
   and exponent notation reads like a bug in a price field. 0.0₆295 is the
   notation every Solana chart uses, and it stays honest at both ends of the
   scale — so one formatter serves both the $8 token and the sub-cent one. */
function fmtPrice(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return '—';
  if (n >= 1)     return `$${n.toFixed(2)}`;
  if (n >= 0.01)  return `$${n.toFixed(4)}`;
  if (n >= 1e-4)  return `$${n.toFixed(6)}`;
  const exp = Math.floor(Math.log10(n));            // -7 for 2.95e-7
  const zeros = -exp - 1;                           // zeros after "0."
  const digits = String(Math.round(n * 10 ** (-exp + 3))).slice(0, 4);
  const marker = String(zeros).split('').map((d) => SUB[+d]).join('');
  return `$0.0${marker}${digits}`;
}

const fmtPct = (v) => {
  const n = Number(v || 0);
  return `${n >= 0 ? '▲' : '▼'} ${Math.abs(n).toFixed(2)}%`;
};

/* One renderer, two charts. The pattern id has to be per-chart: two <pattern>
   elements sharing id="hatch" in one document means the second chart silently
   paints itself with the first chart's colour. */
function makeChart(svgId, tipId, labelFor) {
  let data = [];
  const W = 600, H = 220, pad = 8;
  const hatchId = `hatch-${svgId}`;

  const geom = () => {
    const vals = data.map((p) => p.c);
    const min = Math.min(...vals), max = Math.max(...vals);
    const span = (max - min) || Math.abs(max) || 1;
    return {
      x: (i) => (i / (data.length - 1 || 1)) * W,
      y: (v) => H - pad - ((v - min) / span) * (H - pad * 2),
    };
  };

  function draw() {
    const svg = $(svgId);
    svg.onmousemove = svg.onmouseleave = null;
    if (!data.length) { svg.innerHTML = ''; return; }

    const { x, y } = geom();
    const up = data[data.length - 1].c >= data[0].c;
    const stroke = up ? GREEN : RED;
    const baseline = `<line x1="0" y1="${H - pad}" x2="${W}" y2="${H - pad}"
                            stroke="${INK}" stroke-width="1" opacity=".35"/>`;

    // A token minutes old can have exactly one candle. A one-point path draws
    // nothing at all, so mark the level instead of rendering an empty box.
    if (data.length === 1) {
      const yy = H / 2;
      svg.innerHTML = `${baseline}
        <line x1="0" y1="${yy}" x2="${W}" y2="${yy}" stroke="${stroke}"
              stroke-width="2" stroke-dasharray="6 5" opacity=".65"/>
        <circle cx="${W / 2}" cy="${yy}" r="5" fill="${stroke}"/>`;
      return;
    }

    const line = data.map((p, i) =>
      `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.c).toFixed(1)}`).join('');

    // Hatched fill rather than a gradient — reads as engraving, not dashboard.
    svg.innerHTML = `
      <defs>
        <pattern id="${hatchId}" width="6" height="6" patternUnits="userSpaceOnUse"
                 patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="6" stroke="${stroke}" stroke-width="1.6"
                opacity=".22"/>
        </pattern>
      </defs>
      ${baseline}
      <path d="${line}L${W},${H - pad}L0,${H - pad}Z" fill="url(#${hatchId})"/>
      <path d="${line}" fill="none" stroke="${stroke}" stroke-width="2.4"
            vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
      <circle class="dot" r="4" fill="${stroke}" opacity="0"/>`;

    svg.onmousemove = (ev) => {
      const rect = svg.getBoundingClientRect();
      const i = Math.max(0, Math.min(data.length - 1,
        Math.round(((ev.clientX - rect.left) / rect.width) * (data.length - 1))));
      const p = data[i];
      if (!p) return;
      const dot = svg.querySelector('.dot');
      dot.setAttribute('cx', x(i)); dot.setAttribute('cy', y(p.c));
      dot.setAttribute('opacity', '1');
      const tip = $(tipId);
      tip.hidden = false;
      tip.textContent = `${fmtPrice(p.c)} — ${labelFor(p.t)}`;
      tip.style.left = `${Math.min(ev.clientX - rect.left, rect.width - 150)}px`;
      tip.style.top = `${Math.max(0, ev.clientY - rect.top - 32)}px`;
    };
    svg.onmouseleave = () => {
      $(tipId).hidden = true;
      svg.querySelector('.dot')?.setAttribute('opacity', '0');
    };
  }

  return { set(d) { data = d || []; draw(); } };
}

const marketChart = makeChart('chart', 'chartTip',
  (t) => new Date(t).toLocaleDateString());
const memeChart = makeChart('memeChart', 'memeTip',
  (t) => new Date(t).toLocaleString([], { month: 'short', day: 'numeric',
                                          hour: '2-digit', minute: '2-digit' }));

// Chips are scoped per chart — a bare `.chip` selector would clear the other
// chart's active state and reload the wrong series.
document.querySelectorAll('.chips').forEach((group) => {
  group.querySelectorAll('.chip').forEach((chip) => {
    chip.onclick = () => {
      group.querySelectorAll('.chip').forEach((c) => c.classList.remove('active'));
      chip.classList.add('active');
      if (group.dataset.chart === 'meme') loadMemecoin(chip.dataset.window);
      else loadChart(Number(chip.dataset.days));
    };
  });
});

async function loadChart(days) {
  try {
    const d = await (await api(`/api/market/TRUMP?days=${days}`)).json();
    $('chartLast').textContent = fmtPrice(d.last);
    const chg = Number(d.change_pct || 0);
    const el = $('chartChange');
    el.textContent = fmtPct(chg);
    el.className = 'q-chg ' + (chg >= 0 ? 'up' : 'down');
    // Say plainly when the series is invented. A chart passing synthetic
    // numbers off as market data would be worse than no chart.
    $('chartSource').textContent = d.source === 'synthetic'
      ? 'Demo data — live quote unavailable'
      : `Source: ${d.source}`;
    marketChart.set(d.series);
  } catch {
    $('chartSource').textContent = 'Quote unavailable';
  }
}

async function loadMemecoin(window) {
  const src = $('memeSource');
  try {
    const d = await (await api(`/api/memecoin?window=${encodeURIComponent(window)}`)).json();
    $('memeSym').textContent = `$${d.symbol || 'COIN'}`;
    if (d.url) { $('memeLink').href = d.url; }

    // No data is a state we show honestly rather than paper over: this is a
    // real token and an invented line could cost somebody money.
    if (d.unavailable || !(d.series || []).length) {
      memeChart.set([]);
      $('memeLast').textContent = '—';
      $('memeChange').textContent = '';
      $('memeMeta').hidden = true;
      src.textContent = d.unavailable === 'no trades in this window'
        ? 'No trades in this window yet.'
        : `Live price unavailable (${d.unavailable || 'no data'}).`;
      return;
    }

    $('memeLast').textContent = fmtPrice(d.last);
    const chg = Number(d.change_pct || 0);
    const el = $('memeChange');
    el.textContent = fmtPct(chg);
    el.className = 'q-chg ' + (chg >= 0 ? 'up' : 'down');
    memeChart.set(d.series);

    $('memeStats').textContent =
      `High ${fmtPrice(d.high)} · Low ${fmtPrice(d.low)} · `
      + `Volume $${Math.round(d.volume || 0).toLocaleString()}`;
    $('memeMeta').hidden = false;
    src.textContent = d.points === 1
      ? 'Source: GeckoTerminal — one candle so far, too new to plot a line.'
      : `Source: GeckoTerminal · ${d.points} candles`;
  } catch {
    memeChart.set([]);
    src.textContent = 'Live price unavailable.';
    $('memeMeta').hidden = true;
  }
}

boot();

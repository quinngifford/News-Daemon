/* Dashboard: SSE live feed, health polling, Web Push enrolment.
 *
 * The SSE stream is the lowest-latency channel in the whole system — no vendor
 * push infrastructure in the path — and it is the only one that can guarantee an
 * audible alert, since no push service can promise the OS will make a sound.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const seen = new Set();
let soundOn = false;
let audioCtx = null;

// ---------- audible alarm ----------------------------------------------------

function beep(times = 3) {
  if (!soundOn) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    for (let i = 0; i < times; i++) {
      const t0 = audioCtx.currentTime + i * 0.42;
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'square';
      osc.frequency.setValueAtTime(880, t0);
      osc.frequency.setValueAtTime(1180, t0 + 0.16);
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.28, t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.36);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(t0);
      osc.stop(t0 + 0.38);
    }
  } catch (e) {
    console.warn('beep failed', e);
  }
}

$('soundBtn').onclick = () => {
  soundOn = !soundOn;
  $('soundBtn').textContent = 'sound: ' + (soundOn ? 'ON' : 'off');
  // Unlock the AudioContext on this user gesture; browsers block autoplay
  // otherwise, and discovering that during a real alert is too late.
  if (soundOn) beep(1);
};

// ---------- live alerts ------------------------------------------------------

function renderAlert(a, isNew) {
  const box = $('alerts');
  if (box.querySelector('.empty')) box.innerHTML = '';
  const when = new Date((a.t_wall || Date.now() / 1000) * 1000)
    .toISOString().replace('T', ' ').slice(0, 19);
  const lat = a.detect_latency_ms != null
    ? `${(a.detect_latency_ms / 1000).toFixed(2)}s` : '—';

  const row = document.createElement('div');
  row.className = 'row' + (isNew ? ' new' : '');
  row.innerHTML = `
    <span class="tag ${a.state}">${a.state}</span>
    <span style="flex:1">
      <strong>${escapeHtml(a.target || '')}</strong>
      — ${escapeHtml(a.headline || '')}
      ${a.url ? `<a href="${encodeURI(a.url)}" target="_blank"
                    rel="noopener" style="color:var(--accent)"> ↗</a>` : ''}
      <div class="mono-dim">${when} · detect ${lat} · score ${
        a.score != null ? Number(a.score).toFixed(3) : '?'}</div>
    </span>`;
  box.prepend(row);
  while (box.children.length > 60) box.removeChild(box.lastChild);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function connect() {
  const es = new EventSource('/api/events');
  es.onopen = () => setPill('conn', 'live', 'ok');
  es.onerror = () => setPill('conn', 'reconnecting…', 'warn');
  es.onmessage = (ev) => {
    let a;
    try { a = JSON.parse(ev.data); } catch { return; }
    if (a.type !== 'alert') return;
    const key = a.event_id + ':' + a.state;
    if (seen.has(key)) return;      // history replay on reconnect
    seen.add(key);
    renderAlert(a, true);
    if (a.state === 'confirmed') beep(5);
    else if (a.state === 'likely') beep(2);
  };
}

function setPill(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className = 'pill' + (cls ? ' ' + cls : '');
}

// ---------- health polling --------------------------------------------------

async function poll() {
  try {
    const h = await (await fetch('/api/health')).json();
    setPill('healthPill',
      h.ok ? 'health ok' : `health ${h.stale_sources.length} stale`,
      h.ok ? 'ok' : 'bad');

    if (h.canary) {
      const mins = h.canary.age_s != null ? Math.round(h.canary.age_s / 60) : '?';
      setPill('canaryPill',
        `canary ${h.canary.passed ? 'pass' : 'FAIL'} ${mins}m ago`,
        h.canary.passed ? 'ok' : 'bad');
    } else {
      setPill('canaryPill', 'canary never run', 'warn');
    }

    const st = $('sources').querySelector('tbody');
    st.innerHTML = h.sources.map((s) => `<tr>
      <td>${escapeHtml(s.id)}</td>
      <td>T${s.tier}</td>
      <td>${s.items} items</td>
      <td style="color:${s.stale ? 'var(--bad)' : 'var(--dim)'}">${
        s.stale ? 'STALE' : Math.round(s.staleness_s) + 's'}</td>
      <td style="color:var(--bad)">${escapeHtml(s.last_error || '')}</td>
    </tr>`).join('');

    const ft = $('funnel').querySelector('tbody');
    ft.innerHTML = Object.entries(h.funnel).map(([k, v]) =>
      `<tr><td>${escapeHtml(k)}</td><td>${v}</td></tr>`).join('');

    const stt = await (await fetch('/api/status')).json();
    const tt = Object.entries(stt.targets || {});
    $('targets').innerHTML = tt.length ? tt.map(([id, t]) => `
      <div class="row">
        <span class="tag ${t.state}">${t.state}</span>
        <span style="flex:1"><strong>${escapeHtml(id)}</strong>
          <div class="mono-dim">weight ${t.weight} · ${
            t.evidence_count} evidence · origins: ${
            (t.origins || []).join(', ') || 'none'}</div></span>
      </div>`).join('') : '<div class="empty">no targets</div>';
  } catch (e) {
    setPill('healthPill', 'health unreachable', 'bad');
  }
}

// ---------- web push --------------------------------------------------------

function b64ToU8(b64) {
  const pad = '='.repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function initPush() {
  const btn = $('pushBtn');
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    btn.textContent = 'push unsupported';
    return;
  }
  let reg;
  try {
    reg = await navigator.serviceWorker.register('/sw.js');
  } catch (e) {
    btn.textContent = 'sw failed';
    console.error(e);
    return;
  }

  const existing = await reg.pushManager.getSubscription();
  if (existing) {
    btn.textContent = 'push enabled';
    btn.disabled = true;
    return;
  }
  btn.disabled = false;

  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = 'enabling…';
    try {
      if (await Notification.requestPermission() !== 'granted') {
        btn.textContent = 'permission denied';
        return;
      }
      const { publicKey } = await (await fetch('/api/vapid-public-key')).json();
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: b64ToU8(publicKey),
      });
      const j = sub.toJSON();
      const res = await fetch('/api/subscribe', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ endpoint: j.endpoint, keys: j.keys }),
      });
      if (!res.ok) throw new Error('subscribe failed: ' + res.status);
      btn.textContent = 'push enabled';
    } catch (e) {
      console.error(e);
      btn.textContent = 'push failed';
      btn.disabled = false;
    }
  };
}

// Existing alerts first so a reload is not a blank page, then go live.
(async () => {
  try {
    const { alerts } = await (await fetch('/api/alerts?limit=30')).json();
    (alerts || []).reverse().forEach((a) => {
      seen.add(a.event_id + ':' + a.state);
      renderAlert({
        state: a.state, target: a.target_id, headline: a.headline,
        url: a.url, score: a.score, t_wall: a.t_wall,
        detect_latency_ms: a.detect_latency_ms,
      }, false);
    });
  } catch (e) { /* first boot, empty db */ }
  connect();
  initPush();
  poll();
  setInterval(poll, 5000);
})();

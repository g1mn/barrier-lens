/* Barrier Lens front end: map of Gwanghwamun & Gyeongbokgung + "Check a spot" flow. No build step. */
(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const CENTRE = [37.5759283, 126.9768176];  // Gwanghwamun Gate; the map opens here
  const params = new URLSearchParams(location.search);
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const VERDICT_WORD = { passes: 'Passes', tight: 'Tight', blocked: 'Blocked', unknown: 'Unknown' };
  const MASK_COLORS = ['#ffd23f', '#4cc9f0', '#ff6bd6', '#7ee081', '#ff8c42'];

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const monthLabel = (d) => (d ? `${MONTHS[Number(d.slice(5, 7)) - 1]} ${d.slice(0, 4)}` : '');
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode: ignore */ } },
  };

  // Live (paid) checks on a public deploy need the judge token; the server says so in /api/health.
  const TOKEN_KEY = 'bl-demo-token';
  function liveHeaders() {
    const t = ($('#demo-token') && $('#demo-token').value.trim()) || store.get(TOKEN_KEY) || '';
    return t ? { 'X-Demo-Token': t } : {};
  }
  fetch('/api/health').then((r) => r.json()).then((h) => {
    if (!h.live_needs_token) return;
    $('#token-row').hidden = false;
    const inp = $('#demo-token');
    inp.value = store.get(TOKEN_KEY) || '';
    inp.addEventListener('change', () => store.set(TOKEN_KEY, inp.value.trim()));
  }).catch(() => { /* offline: the sample still works */ });

  // ------------------------------------------------------------------ theme
  const themeBtn = $('#theme-toggle');
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    const dark = t === 'dark';
    themeBtn.setAttribute('aria-pressed', String(dark));
    themeBtn.setAttribute('aria-label', dark ? 'Dark theme on. Switch to light theme' : 'Light theme on. Switch to dark theme');
  }
  applyTheme(store.get('bl-theme') === 'light' ? 'light' : 'dark');
  themeBtn.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    store.set('bl-theme', next);
  });

  // ------------------------------------------------------------------ map
  const map = L.map('map', { zoomControl: false, attributionControl: true }).setView(CENTRE, 16);
  L.control.zoom({ position: 'topright' }).addTo(map);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  // keep: replaces Leaflet's default flag-emoji prefix with a plain Leaflet link.
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  map.attributionControl.addAttribution('Towing, elevators: <a href="https://data.seoul.go.kr/">Seoul Open Data</a> (KOGL Type 1)');

  const towLayer = L.layerGroup().addTo(map);
  const exitLayer = L.layerGroup().addTo(map);
  const pinLayer = L.layerGroup().addTo(map);
  let ring = null;

  // ------------------------------------------------------------------ layout: bar height, sheet, crosshair
  const sheet = $('#sheet');
  const handle = $('#sheet-handle');
  const crosshair = $('#crosshair');

  function barH() { return $('.bar').offsetHeight; }
  function isWide() { return window.matchMedia('(min-width: 900px)').matches; }
  // Narrow screens: the bottom sheet covers the bottom-right corner, so the credit line moves to the top.
  let narrowCredit = null;
  function placeAttribution() {
    const narrow = !isWide();
    if (narrow === narrowCredit) return;
    narrowCredit = narrow;
    map.attributionControl.setPosition(narrow ? 'topleft' : 'bottomright');
  }
  function visibleMapBox() {
    const top = barH();
    if (isWide()) {
      const r = sheet.getBoundingClientRect();
      return { left: r.right, top, right: window.innerWidth, bottom: window.innerHeight };
    }
    return { left: 0, top, right: window.innerWidth, bottom: sheet.getBoundingClientRect().top };
  }
  function layout() {
    document.documentElement.style.setProperty('--bar-h', `${barH()}px`);
    placeAttribution();
    const b = visibleMapBox();
    crosshair.style.left = `${(b.left + b.right) / 2}px`;
    crosshair.style.top = `${(b.top + b.bottom) / 2}px`;
    map.invalidateSize();
  }
  // Pan so that latlng sits in the middle of the part of the map the sheet does not cover.
  function centreOn(latlng, zoom) {
    if (zoom) map.setView(latlng, zoom, { animate: false });
    const b = visibleMapBox();
    const mapRect = map.getContainer().getBoundingClientRect();
    const want = L.point((b.left + b.right) / 2 - mapRect.left, (b.top + b.bottom) / 2 - mapRect.top);
    const now = map.latLngToContainerPoint(latlng);
    map.panBy(now.subtract(want), { animate: !zoom });
  }
  function crosshairLatLng() {
    const r = crosshair.getBoundingClientRect();
    const m = map.getContainer().getBoundingClientRect();
    return map.containerPointToLatLng(L.point(r.left + r.width / 2 - m.left, r.top + r.height / 2 - m.top));
  }
  function setSheet(state) {
    sheet.dataset.state = state;
    const open = state !== 'peek';
    handle.setAttribute('aria-expanded', String(open));
    $('#sheet-handle-label').textContent = open ? 'Collapse panel' : 'Expand panel';
    requestAnimationFrame(layout);
  }
  handle.addEventListener('click', () => setSheet(sheet.dataset.state === 'peek' ? 'half' : 'peek'));
  window.addEventListener('resize', layout);
  if ('ResizeObserver' in window) new ResizeObserver(() => layout()).observe(sheet);

  // ------------------------------------------------------------------ towing history
  async function loadTowing(r) {
    const stat = $('#towing-stat');
    stat.textContent = 'Loading towing history…';
    let d;
    try {
      const res = await fetch(`/api/towing?lat=${CENTRE[0]}&lon=${CENTRE[1]}&r=${r}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      d = await res.json();
    } catch (e) {
      stat.textContent = `Could not load towing history (${e.message}).`;
      return;
    }
    towLayer.clearLayers();
    if (ring) ring.remove();
    ring = L.circle(CENTRE, { radius: r, color: '#111', weight: 2, dashArray: '6 6', fill: false, interactive: false }).addTo(map);

    // no per-address coordinates are stored, so only the ring is drawn
    const pts = d.points || [];

    const cov = d.coverage || {};
    if (!pts.length) {
      stat.textContent = cov.records == null
        ? 'Towing history is not cached yet (run scripts/fetch_seoul.py).'
        : `No towing reports in the cached data within ${r} m.`;
    } else {
      const first = pts.reduce((a, p) => (p.first < a ? p.first : a), pts[0].first);
      const last = pts.reduce((a, p) => (p.last > a ? p.last : a), pts[0].last);
      stat.innerHTML = `<strong>${d.records.toLocaleString('en')}</strong> e-scooter towing reports at ${pts.length} addresses within ${r >= 1000 ? `${r / 1000} km` : `${r} m`} of Gwanghwamun Gate · ${monthLabel(first)} – ${monthLabel(last)}`;
    }

    const list = $('#hotspots');
    list.innerHTML = '';
    [...pts].sort((a, b) => b.count - a.count).slice(0, 5).forEach((p) => {
      const li = document.createElement('li');
      li.innerHTML = `<span class="addr" lang="ko">${esc(shortAddr(p.addr))}<span class="meta" lang="en">within ${p.band_m} m of Gwanghwamun Gate · last ${monthLabel(p.last)}</span></span>
        <span class="n">${p.count}<span class="visually-hidden"> reports</span></span>`;
      list.appendChild(li);
    });

    const cav = $('#caveats');
    cav.innerHTML = '';
    [...(cov.caveats || []), cov.source ? `Source: ${cov.source}.` : null].filter(Boolean).forEach((t) => {
      const li = document.createElement('li');
      li.textContent = t;
      cav.appendChild(li);
    });
  }
  function shortAddr(a) { return a.replace(/^서울특별시\s+/, ''); }

  document.querySelectorAll('input[name="radius"]').forEach((el) => {
    el.addEventListener('change', () => { if (el.checked) loadTowing(Number(el.value)); });
  });

  // ------------------------------------------------------------------ station exits + elevator status
  async function loadStation() {
    const line = $('#station-line');
    let d;
    try {
      const res = await fetch('/api/station?live=1');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      d = await res.json();
    } catch (e) {
      line.textContent = `Exit elevator status unavailable (${e.message}).`;
      return;
    }
    exitLayer.clearLayers();
    (d.exits || []).forEach((x) => {
      const cls = x.elevator === 'working' ? 'ev-working' : x.elevator === 'out of service' ? 'ev-out' : '';
      const icon = L.divIcon({ className: '', html: `<div class="exit-pin ${cls}">${esc(x.ref)}</div>`, iconSize: [30, 30], iconAnchor: [15, 15] });
      const rows = (x.elevator_rows || []).map((r) => `<li lang="ko">${esc(r.name.replace(/^승강기\)/, ''))}: <strong>${esc(r.status)}</strong></li>`).join('');
      const body = x.elevator === 'none listed'
        ? '<p>No elevator is listed for this exit in the Seoul Metro feed.</p>'
        : `<p>Elevator <strong>${esc(x.elevator)}</strong></p><ul>${rows}</ul>`;
      const label = `${x.station_name} exit ${x.ref}`;
      L.marker([x.lat, x.lon], { icon, title: `${label}: elevator ${x.elevator}`, keyboard: true, zIndexOffset: cls ? 500 : 0 })
        .bindPopup(`<h4>${esc(label)}</h4>${body}<p class="ko">Status ${esc(d.status_source)} · ${esc(fmtTime(d.status_as_of))}</p>`)
        .addTo(exitLayer);
    });
    const parts = (d.stations || []).map((s) => {
      const withLift = (s.exits || []).filter((x) => x.elevator === 'working' || x.elevator === 'out of service');
      const bit = !withLift.length ? 'no exit elevator listed'
        : withLift.map((x) => (x.elevator === 'working'
          ? `exit ${x.ref} elevator working`
          : `<strong>exit ${x.ref} out of service</strong>`)).join(', ');
      return `${esc(s.name)} (${esc(s.line)}): ${bit}`;
    });
    line.innerHTML = `${parts.join(' · ')} <span class="asof">(${esc(d.status_source)}, ${esc(fmtTime(d.status_as_of))})</span>`;
  }
  function fmtTime(iso) {
    if (!iso) return 'time unknown';
    const t = new Date(iso.replace(/([+-]\d{2})(\d{2})$/, '$1:$2'));
    if (Number.isNaN(t.getTime())) return iso;
    return t.toLocaleString('en-GB', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Seoul' }) + ' KST';
  }

  // ------------------------------------------------------------------ check a spot
  const panelOverview = $('#panel-overview');
  const panelCheck = $('#panel-check');
  const clip = $('#clip');
  const video = $('#video');
  const canvas = $('#overlay');
  const ctx = canvas.getContext('2d');
  const stage = $('#stage');
  const status = $('#check-status');
  const check = { result: null, sample: false, pin: null, playing: false, t0: 0, raf: 0, url: null, maskCache: new Map() };

  function enterCheck() {
    panelOverview.hidden = true;
    panelCheck.hidden = false;
    $('#check-btn').hidden = true;
    crosshair.hidden = false;
    setSheet('half');
    clip.focus();
  }
  function leaveCheck() {
    panelCheck.hidden = true;
    panelOverview.hidden = false;
    $('#check-btn').hidden = false;
    crosshair.hidden = true;
    setSheet('peek');
    $('#check-btn').focus();
  }
  $('#check-btn').addEventListener('click', enterCheck);
  $('#back-btn').addEventListener('click', leaveCheck);

  clip.addEventListener('change', () => {
    const f = clip.files && clip.files[0];
    resetResult();
    if (!f) { $('#analyze-btn').disabled = true; return; }
    if (check.url) URL.revokeObjectURL(check.url);
    check.url = URL.createObjectURL(f);
    video.src = check.url;
    stage.hidden = false;
    stage.classList.remove('no-video');
    $('#analyze-btn').disabled = false;
    setStatus(`Clip ready: ${f.name} (${(f.size / 1e6).toFixed(1)} MB). Place the pin, then measure.`);
  });
  video.addEventListener('loadedmetadata', () => {
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    draw();
  });

  $('#analyze-btn').addEventListener('click', async () => {
    const f = clip.files && clip.files[0];
    if (!f) return;
    const at = crosshairLatLng();
    const fd = new FormData();
    fd.append('video', f);
    fd.append('lat', at.lat.toFixed(6));
    fd.append('lon', at.lng.toFixed(6));
    if (profile) fd.append('profile', JSON.stringify(profile));
    const s = tilt.read(f);
    if (s) fd.append('sensor', JSON.stringify(s));
    $('#analyze-btn').disabled = true;
    setStatus('Measuring… this takes about a minute. Each step below shows when it finishes.');
    const result = await streamCheck(fd);
    $('#analyze-btn').disabled = false;
    if (result) {
      useClip(result.video_url);
      showResult(result, false, at);
      setStatus(statusLine(result));
    }
  });


  // Live check as a stream of stage events (POST /api/analyze/stream), shown in the #stages list as they happen.
  const STAGE_INFO = {
    blur: (e) => (e.faces == null ? '' : `${e.faces} face detection${e.faces === 1 ? '' : 's'} blurred in ${e.frames} frames`),
    triage: (e) => [e.usable === false ? 'clip not usable' : '', e.path ? `path ${e.path}` : '', (e.barriers || []).join(', ')].filter(Boolean).join(' · '),
    sam: (e) => Object.entries(e.outlined || {}).map(([c, n]) => `${c} ${n} frames`).join(', '),
    measure: (e) => [e.verdict, e.mode].filter(Boolean).join(' · '),
    explain: (e) => (e.model === 'template' ? 'fixed sentence (model text failed a check)' : ''),
  };
  function stageReset() {
    const ol = $('#stages');
    ol.hidden = false;
    ol.querySelectorAll('li').forEach((li) => { li.dataset.state = 'todo'; li.querySelector('.st').textContent = ''; });
  }
  function stageEvent(e) {
    const li = $(`#stages li[data-stage="${e.stage}"]`);
    if (!li) return;
    li.dataset.state = e.status === 'start' ? 'run' : 'done';
    if (e.status === 'done') {
      const more = (STAGE_INFO[e.stage] || (() => ''))(e);
      li.querySelector('.st').textContent = ` — ${e.s ?? ''}s${more ? ` · ${more}` : ''}`;
    }
  }
  async function streamCheck(fd) {
    stageReset();
    let res;
    try {
      res = await fetch('/api/analyze/stream', { method: 'POST', body: fd, headers: liveHeaders() });
    } catch (e) {
      setStatus(`Could not reach the server (${e.message}).`, true);
      return null;
    }
    if (!res.ok || !res.body) {
      let detail = '';
      try { detail = (await res.json()).detail; } catch { /* not JSON */ }
      setStatus(`Could not measure: ${typeof detail === 'string' && detail ? detail : `HTTP ${res.status}`}`, true);
      return null;
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '', result = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        if (ev.stage) stageEvent(ev);
        else if (ev.result) result = ev.result;
        else if (ev.error) { setStatus(`Could not measure: ${ev.error}`, true); return null; }
      }
    }
    if (!result) setStatus('The check ended without a result. Please try again.', true);
    return result;
  }

  // GET/POST JSON; on failure, says why in the status line and returns null.
  async function getJson(url, opts) {
    let res;
    try {
      res = await fetch(url, opts);
    } catch (e) {
      setStatus(`Could not reach the server (${e.message}).`, true);
      return null;
    }
    if (res.ok) return res.json();
    let detail = '';
    try { detail = (await res.json()).detail; } catch { /* not JSON */ }
    setStatus(`Could not measure: ${typeof detail === 'string' && detail ? detail : `HTTP ${res.status}`}`, true);
    return null;
  }

  // Show the server's face-blurred, normalised clip (the masks refer to its frames), or no video.
  function useClip(url) {
    stage.hidden = false;
    if (url) {
      if (check.url) { URL.revokeObjectURL(check.url); check.url = null; }
      stage.classList.remove('no-video');
      if (video.getAttribute('src') !== url) video.src = url;
    } else {
      stage.classList.add('no-video');
      video.removeAttribute('src');
    }
  }

  // Sample and recorded clips were filmed elsewhere; the pin sits at the crosshair, not where they were filmed.
  function clipOrigin(r) {
    if (!r.sample && !r.recorded) return '';
    return (r.recorded && r.recorded.about) || 'Sample clip filmed elsewhere — the pin position is illustrative';
  }
  function statusLine(r) {
    const b = r.blur || {};
    const faces = b.faces == null ? '' : ` Face detections blurred before upload: ${b.faces} in ${b.frames_with_faces} of ${b.frames} frames.`;
    const cost = !r.cost ? '' : (r.recorded && r.cost.sam_frames === 0)
      ? ' SAM masks reused from an earlier live call of this clip (not billed again).'
      : ` ${r.recorded ? 'SAM frames billed when it was recorded' : 'Paid SAM frames this check'}: ${r.cost.sam_frames} ($${Number(r.cost.usd_estimate || 0).toFixed(2)}).`;
    const head = r.recorded ? `${r.recorded.about ? `${r.recorded.about} ` : ''}Recorded live run (${r.models.triage} and ${r.models.sam} on the Meta Model API, ${r.recorded.at.slice(0, 10)}), replayed with no model calls now.`
      : r.sample ? 'Sample clip, replayed results (no live model calls).' : 'Done.';
    return `${head}${faces}${cost}`;
  }

  const sampleBtns = [$('#sample-btn'), $('#sample-closed-btn'), $('#sample-known-btn')];
  sampleBtns.forEach((btn) => btn.addEventListener('click', async () => {
    const at = crosshairLatLng();
    if (params.get('verdict')) { await runSample(false, at); return; }  // hand-made UI fixture variants
    sampleBtns.forEach((x) => { x.disabled = true; });
    setStatus('Loading the sample clip…');
    const sfd = new FormData();
    sfd.append('lat', at.lat.toFixed(6));
    sfd.append('lon', at.lng.toFixed(6));
    sfd.append('name', btn.dataset.sample);
    if (profile) sfd.append('profile', JSON.stringify(profile));
    const result = await getJson('/api/sample', { method: 'POST', body: sfd });
    sampleBtns.forEach((x) => { x.disabled = false; });
    if (!result) return;
    clip.value = '';
    $('#analyze-btn').disabled = true;
    $('#stages').hidden = true;
    resetResult();
    useClip(result.video_url);
    if (!result.video_url) { canvas.width = result.frame_size[0]; canvas.height = result.frame_size[1]; }
    showResult(result, true, at);
    setStatus(statusLine(result) + (result.precomputed ? ' The clip itself is not on this server, so only the masks are shown.' : ''));
  }));
  $('#again-btn').addEventListener('click', () => {
    clip.value = '';
    if (check.url) { URL.revokeObjectURL(check.url); check.url = null; }
    video.removeAttribute('src');
    video.load();
    stage.hidden = true;
    $('#stage-ctl').hidden = true;
    resetResult();
    $('#analyze-btn').disabled = true;
    setStatus('');
    clip.focus();
  });
  $('#confirm-btn').addEventListener('click', () => {
    if (!check.pin) return;
    check.pin.confirmed = true;
    check.pin.marker.setIcon(pinIcon(check.pin.verdict, true));
    check.pin.marker.dragging?.disable();
    $('#confirm-btn').disabled = true;
    $('#confirm-btn').textContent = 'Pin confirmed';
    setStatus('Pin confirmed on this screen. Sharing pins with others is not built yet.');
  });

  function setStatus(text, warn = false) {
    status.textContent = text;
    status.classList.toggle('warn', warn);
  }
  function resetResult() {
    check.result = null;
    check.maskCache.clear();
    stopLoop();
    $('#verdict').hidden = true;
    $('#mask-legend').innerHTML = '';
    clearCanvas();
  }

  let fixture = null;
  async function runSample(keepVideo, at) {
    if (!fixture) {
      const res = await fetch('/static/fixtures/sample-check.json');
      fixture = await res.json();
    }
    const variant = fixture.variants[params.get('verdict')] ? params.get('verdict') : fixture.default_variant;
    const v = fixture.variants[variant];
    const m = v.measurement || {};
    const result = {
      masks: fixture.sam, verdict: m.verdict, gap_cm: m.gap_cm, band_cm: m.band_cm, why: m.why,
      explanation: v.explanation && v.explanation.text, handmade: true,
    };
    if (!keepVideo) {
      stage.hidden = false;
      stage.classList.add('no-video');
      const s0 = fixture.sam[0];
      canvas.width = s0.width;
      canvas.height = s0.height;
    }
    showResult(result, true, at);
    if (!keepVideo) setStatus('Sample: hand-made masks and numbers, not a measurement.');
  }

  function showResult(result, sample, at) {
    check.result = result;
    check.sample = sample;
    check.maskCache.clear();
    const tracks = toTracks(result);
    const shown = tracks.filter((t) => t.frames.length);
    $('#mask-legend').innerHTML = (sample ? '<span class="sample-tag">Sample</span>' : '')
      + (shown.length ? shown.map((t) => `<span><i style="background:${t.color}"></i>${esc(t.concept)}${t.role === 'reference' ? ' (scale reference)' : ''}</span>`).join('')
        : '<span>No masks found</span>');
    $('#stage-ctl').hidden = false;
    renderVerdict(result, sample);
    dropPin(result, at);
    if (stage.classList.contains('no-video')) { check.playing = false; togglePlay(); } else { draw(); video.play().catch(() => {}); }
    loopForVideo();
    if (!tourRunning) {  // the tour keeps focus and scrolls on its own card instead
      $('#verdict').focus({ preventScroll: true }); $('#verdict').scrollIntoView({ block: 'start' });  // card taller than the sheet: show its verdict word, not its middle
    }
  }

  // result.masks: [{concept, role, source, frames: [{i, objects: [{id, polygon, polygons}]}]}] in frame_size pixels.
  function toTracks(result) {
    const list = result.masks || result.sam || [];
    const [w, h] = result.frame_size || [];
    return (Array.isArray(list) ? list : [list]).map((t, k) => ({
      ...t,
      width: t.width || w,
      height: t.height || h,
      fps: t.fps || result.fps,
      color: t.role === 'reference' ? '#7ee081' : MASK_COLORS[k % MASK_COLORS.length],
      frames: [...(t.frames || [])].sort((a, b) => a.i - b.i),
    }));
  }


  const BAR_MAX_CM = 300;  // ranges beyond this are drawn to the edge and labelled "over 3 m"
  function bandText(b) {
    if (b[1] > BAR_MAX_CM) return `${Math.round(b[0])} cm – over ${BAR_MAX_CM / 100} m`;
    return `${Math.round(b[0])}–${Math.round(b[1])} cm`;   // short enough for a half-width tile on a phone
  }
  // One bar: the measured range against the chair width and the width needed with clearance.
  function renderBar(r) {
    const fig = $('#v-bar');
    const band = Array.isArray(r.band_cm) ? r.band_cm : null;
    const d = r.measurement_detail || {};
    const chair = (r.assumptions && r.assumptions.chair_width_cm) || d.wheelchair_cm || 70;
    const need = chair + 2 * (d.clearance_cm ?? 5);
    if (!band) { fig.hidden = true; return; }
    const max = Math.min(BAR_MAX_CM, Math.max(band[1], need) * 1.15);
    const x = (cm) => 8 + (Math.min(cm, max) / max) * 304;
    const svg = $('#v-bar-svg');
    svg.innerHTML =
      `<rect x="8" y="26" width="304" height="12" rx="6" class="bar-track"/>`
      + `<rect x="${x(band[0])}" y="22" width="${Math.max(3, x(band[1]) - x(band[0]))}" height="20" rx="5" class="bar-band"/>`
      + (r.gap_cm != null ? `<line x1="${x(r.gap_cm)}" x2="${x(r.gap_cm)}" y1="18" y2="46" class="bar-gap"/>` : '')
      + `<line x1="${x(chair)}" x2="${x(chair)}" y1="8" y2="56" class="bar-chair"/>`
      + `<line x1="${x(need)}" x2="${x(need)}" y1="8" y2="56" class="bar-need"/>`;
    $('#v-bar-cap').innerHTML = `<span class="k band">Measured range ${esc(bandText(band))}</span>`
      + `<span class="k chair">Your chair ${Math.round(chair)} cm</span><span class="k need">Needed with room ${Math.round(need)} cm</span>`;
    fig.hidden = false;
  }
  function renderNotes(r) {
    const notes = (r.notes || []).filter(Boolean);
    const items = notes.map((n) => `<li>${esc(n)}</li>`);
    if (notes.some((n) => n.includes('CC BY'))) items.push('<li><a href="/static/demo/ATTRIBUTION.txt">SANPO dataset (Google Research), CC BY 4.0 — modified; full credits</a></li>');
    const ul = $('#v-notes');
    ul.innerHTML = items.join('');
    ul.hidden = !items.length;
  }
  // What Muse Spark saw (path state, evidence) next to what SAM 3.1 outlined; says so when they disagree.
  const PATH_WORD = { open: 'Path looks open', narrowed: 'Path is narrowed', closed: 'Path is closed' };
  function renderSaw(r) {
    const tri = r.triage || {};
    const sec = $('#v-saw');
    if (r.handmade || tri.usable == null) { sec.hidden = true; return; }
    const p = tri.path || {};
    $('#v-path').textContent = p.state ? `${PATH_WORD[p.state] || p.state} — ${p.evidence || ''}` : '';
    const sam = Object.fromEntries(toTracks(r).map((t) => [t.concept, t.frames.length]));
    const rows = (tri.barriers || []).map((b) => {
      const n = sam[b.concept];
      const outl = n ? `SAM 3.1 outlined it in ${n} frames` : (n === 0 ? 'SAM 3.1 found no outline for it, so it is not measured' : '');
      return `<li><b>${esc(b.concept)}</b> — ${esc(b.evidence)}${outl ? ` <span class="${n ? 'ok' : 'miss'}">${esc(outl)}</span>` : ''}</li>`;
    });
    $('#v-saw-list').innerHTML = rows.join('');
    sec.hidden = !(rows.length || p.state);
  }


  // An "unknown" that calibration can fix gets a button for it: the tilt sensor, or the one-time calibration.
  const FIX_FOR = {
    tilt: ['Turn on phone tilt', 'The tilt was not recorded. With the sensor on, the next clip carries it.', 'tilt'],
    default_pass: ['Calibrate now', 'Nothing confirmed the scale. Tilt or a one-time calibration does.', 'calib'],
    height: ['Calibrate now', 'Your armrest height sets how high the phone is.', 'calib'],
    'field of view': ['Calibrate now', 'Your camera\u2019s angle of view is not known yet.', 'calib'],
    roll: ['How to hold the phone', 'Hold it level, not tipped to one side.', 'calib'],
  };
  function renderFixit(r, verdict) {
    const p = $('#v-fixit');
    const d = r.measurement_detail || {};
    let fix = verdict === 'unknown' && !r.handmade && FIX_FOR[d.capped || d.dominant_error];
    if (fix && fix[2] === 'tilt' && $('#tilt-row').hidden) {  // no tilt sensor on this device
      fix = ['Calibrate now', 'The tilt was not recorded. A one-time calibration with four A4 sheets on the ground tells us your usual tilt.', 'calib'];
    }
    p.hidden = !fix;
    if (!fix) return;
    $('#fixit-btn').textContent = fix[0];
    $('#fixit-why').textContent = fix[1];
    $('#fixit-btn').dataset.target = fix[2];
  }
  $('#fixit-btn').addEventListener('click', () => {
    if ($('#fixit-btn').dataset.target === 'tilt') {
      $('#tilt-row').scrollIntoView({ block: 'center' });
      $('#tilt-btn').focus();
      if ($('#tilt-btn').getAttribute('aria-pressed') !== 'true') $('#tilt-btn').click();
      return;
    }
    const det = $('#calib');
    det.open = true;
    det.scrollIntoView({ block: 'start' });
    $('#cal-width').focus();
  });

  function renderVerdict(result, sample) {
    const tri = result.triage;
    const verdict = VERDICT_WORD[result.verdict] ? result.verdict : 'unknown';
    const card = $('#verdict');
    card.dataset.verdict = verdict;
    $('#verdict-icon').setAttribute('href', `#v-${verdict}`);
    $('#verdict-text').textContent = VERDICT_WORD[verdict];
    const kicker = $('#verdict-kicker');
    kicker.textContent = result.handmade ? 'Sample · hand-made, not a measurement'
      : sample ? 'Sample clip, filmed elsewhere · replayed model results' : 'Measured from your clip';
    kicker.classList.toggle('sample', sample);
    // unknown: the middle of a too-wide range is not an answer, so it is shown as a best guess, not a headline
    $('#v-nums').classList.toggle('weak', verdict === 'unknown');
    $('#v-nums').hidden = result.gap_cm == null && !Array.isArray(result.band_cm);  // e.g. blocked because the path is closed
    $('#v-gap-label').textContent = verdict === 'unknown' ? 'Best guess (not an answer)' : 'Measured gap';
    $('#v-gap').textContent = result.gap_cm == null ? '—' : `${Math.round(result.gap_cm)} cm`;
    $('#v-band').textContent = Array.isArray(result.band_cm) ? bandText(result.band_cm) : '—';
    renderAssumptions(result);
    renderBar(result);
    renderFixit(result, verdict);
    renderNotes(result);
    renderSaw(result);
    $('#v-why').textContent = (result.why || '').replace(/\(default \((.*?)\)\)/g, '($1)');
    $('#v-explain').textContent = typeof result.explanation === 'string' ? result.explanation : (result.explanation?.text || '');
    const md = result.models || {};
    const srcs = [...new Set(toTracks(result).map((t) => t.source).filter(Boolean))].join(', ');
    const triTxt = tri && tri.usable == null ? 'triage skipped' : `triage ${md.triage || 'unknown'}`;
    $('#v-model').textContent = result.handmade
      ? 'Masks and text are a hand-made fixture for testing the screen.'
      : `Masks ${md.sam || 'SAM'}${srcs ? ` (${srcs})` : ''} · ${triTxt} · text ${md.explain || 'none'} · width from geometry, not from a model.`;
    $('#confirm-btn').disabled = false;
    $('#confirm-btn').textContent = 'Confirm this pin';
    card.hidden = false;
  }

  // ------------------------------------------------------------------ pins
  function pinIcon(verdict, confirmed) {
    return L.divIcon({
      className: '',
      html: `<div class="check-pin v-${verdict}${confirmed ? '' : ' proposed'}"><svg viewBox="0 0 24 24"><use href="#v-${verdict}"></use></svg></div>`,
      iconSize: [36, 36], iconAnchor: [4, 36], popupAnchor: [14, -34],
    });
  }
  function dropPin(result, at) {
    const m = result;
    const verdict = VERDICT_WORD[m.verdict] ? m.verdict : 'unknown';
    const origin = clipOrigin(m);
    if (check.pin && !check.pin.confirmed) pinLayer.removeLayer(check.pin.marker);
    const marker = L.marker(at, { icon: pinIcon(verdict, false), draggable: true, keyboard: true, title: `Checked spot: ${VERDICT_WORD[verdict]}`, zIndexOffset: 1000 })
      .bindPopup(() => `<h4>${VERDICT_WORD[verdict]}${check.sample ? ' (sample)' : ''}</h4>${origin ? `<p>${esc(origin)}</p>` : ''}<p>${m.gap_cm == null ? 'No width measured' : `Gap ${Math.round(m.gap_cm)} cm`}${Array.isArray(m.band_cm) ? ` (${Math.round(m.band_cm[0])}–${Math.round(m.band_cm[1])} cm)` : ''}</p><p class="ko">${check.pin && check.pin.confirmed ? 'Confirmed' : 'Not confirmed yet. Drag to adjust.'}</p>`)
      .addTo(pinLayer);
    check.pin = { marker, verdict, confirmed: false };
  }

  // ------------------------------------------------------------------ mask drawing
  function clearCanvas() { ctx.clearRect(0, 0, canvas.width, canvas.height); }

  // COCO RLE: counts alternate 0-runs and 1-runs, column-major; counts may be a list or the compressed string.
  function rleCounts(counts) {
    if (Array.isArray(counts)) return counts;
    const out = [];
    let p = 0;
    while (p < counts.length) {
      let x = 0; let k = 0; let more = true;
      while (more) {
        const c = counts.charCodeAt(p) - 48;
        x += (c & 0x1f) * 2 ** (5 * k);
        more = (c & 0x20) !== 0;
        p += 1; k += 1;
        if (!more && (c & 0x10)) x -= 2 ** (5 * k);
      }
      if (out.length > 2) x += out[out.length - 2];
      out.push(x);
    }
    return out;
  }
  function rleCanvas(rle, color) {
    const [h, w] = rle.size;
    const cnts = rleCounts(rle.counts);
    const off = document.createElement('canvas');
    off.width = w; off.height = h;
    const octx = off.getContext('2d');
    const img = octx.createImageData(w, h);
    const [r, g, b] = hexRgb(color);
    let idx = 0;
    for (let j = 0; j < cnts.length; j += 1) {
      const n = cnts[j];
      if (j % 2 === 1) {
        for (let q = idx; q < idx + n; q += 1) {
          const x = Math.floor(q / h); const y = q - x * h;
          const o = (y * w + x) * 4;
          img.data[o] = r; img.data[o + 1] = g; img.data[o + 2] = b; img.data[o + 3] = 115;
        }
      }
      idx += n;
    }
    octx.putImageData(img, 0, 0);
    return off;
  }
  function hexRgb(hex) { const n = parseInt(hex.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }

  function frameAt(track, t) {
    const fps = track.fps || 10;
    const want = Math.floor(t * fps + 1e-6);
    let best = null;
    for (const f of track.frames) {
      if (f.i <= want) best = f; else break;
    }
    return best && want - best.i <= fps ? best : null;
  }

  function draw(tOverride) {
    clearCanvas();
    const noVideo = stage.classList.contains('no-video');
    if (noVideo) {
      ctx.fillStyle = '#1b1e24';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#2c313a';
      ctx.fillRect(0, canvas.height * 0.62, canvas.width, canvas.height * 0.38);
    }
    if (!check.result || !$('#mask-toggle').checked) return;
    const t = tOverride ?? (noVideo ? 0 : video.currentTime);
    toTracks(check.result).forEach((track, k) => {
      const { color } = track;
      const f = frameAt(track, t);
      if (!f) return;
      const sx = canvas.width / (track.width || canvas.width);
      const sy = canvas.height / (track.height || canvas.height);
      for (const o of f.objects || []) {
        const polys = o.polygons || (o.polygon ? [o.polygon] : []);
        if (polys.length) {
          ctx.beginPath();
          polys.filter((p) => p.length > 2).forEach((p) => {
            p.forEach(([x, y], n) => (n ? ctx.lineTo(x * sx, y * sy) : ctx.moveTo(x * sx, y * sy)));
            ctx.closePath();
          });
          ctx.fillStyle = `${color}66`;
          ctx.fill('evenodd');
          ctx.lineWidth = Math.max(2, canvas.width / 320);
          ctx.setLineDash(track.role === 'reference' ? [8, 6] : []);
          ctx.strokeStyle = color;
          ctx.stroke();
          ctx.setLineDash([]);
        } else if (o.mask_rle) {
          const key = `${k}:${f.i}:${o.id}`;
          let off = check.maskCache.get(key);
          if (!off) { off = rleCanvas(o.mask_rle, color); check.maskCache.set(key, off); }
          ctx.drawImage(off, 0, 0, canvas.width, canvas.height);
        }
      }
    });
    drawRuler();
  }

  // The measured gap as a ruler between its two ground points (measurement_detail.gap_px, frame pixels).
  function drawRuler() {
    const r = check.result;
    const ends = r && r.measurement_detail && r.measurement_detail.gap_px;
    if (!ends || ends.length !== 2 || r.gap_cm == null) return;
    const [fw, fh] = r.frame_size || [canvas.width, canvas.height];
    const sx = canvas.width / fw, sy = canvas.height / fh;
    const [[x1, y1], [x2, y2]] = ends.map(([x, y]) => [x * sx, y * sy]);
    const lw = Math.max(3, canvas.width / 240);
    const ang = Math.atan2(y2 - y1, x2 - x1), tick = lw * 4;
    const nx = -Math.sin(ang) * tick, ny = Math.cos(ang) * tick;
    ctx.save();
    ctx.lineCap = 'round';
    for (const [w, c] of [[lw + 4, 'rgba(0,0,0,.75)'], [lw, '#ffd23f']]) {
      ctx.lineWidth = w; ctx.strokeStyle = c;
      ctx.beginPath();
      ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
      ctx.moveTo(x1 - nx, y1 - ny); ctx.lineTo(x1 + nx, y1 + ny);
      ctx.moveTo(x2 - nx, y2 - ny); ctx.lineTo(x2 + nx, y2 + ny);
      ctx.stroke();
    }
    const band = Array.isArray(r.band_cm) ? ` (${Math.round(r.band_cm[0])}–${r.band_cm[1] > BAR_MAX_CM ? `${BAR_MAX_CM}+` : Math.round(r.band_cm[1])})` : '';
    const label = `${r.verdict === 'unknown' ? '≈' : ''}${Math.round(r.gap_cm)} cm${band}`;
    ctx.font = `700 ${Math.max(14, canvas.width / 40)}px system-ui, sans-serif`;
    const tw = ctx.measureText(label).width, pad = 6, th = Math.max(14, canvas.width / 40) + pad;
    const lx = Math.min(Math.max((x1 + x2) / 2 - tw / 2 - pad, 0), canvas.width - tw - 2 * pad);
    const ly = Math.max(Math.min(y1, y2) - th - tick, 0);
    ctx.fillStyle = 'rgba(0,0,0,.78)'; ctx.fillRect(lx, ly, tw + 2 * pad, th);
    ctx.fillStyle = '#ffd23f'; ctx.textBaseline = 'top'; ctx.fillText(label, lx + pad, ly + pad / 2);
    ctx.restore();
  }

  // video-synced redraw
  function loopForVideo() {
    if (stage.classList.contains('no-video')) return;
    if ('requestVideoFrameCallback' in HTMLVideoElement.prototype) {
      const tick = () => { draw(); video.requestVideoFrameCallback(tick); };
      video.requestVideoFrameCallback(tick);
    } else {
      const tick = () => { draw(); check.raf = requestAnimationFrame(tick); };
      check.raf = requestAnimationFrame(tick);
    }
  }
  ['seeked', 'pause', 'play'].forEach((ev) => video.addEventListener(ev, () => draw()));

  // no-video sample playback
  const playBtn = $('#play-btn');
  function stopLoop() {
    cancelAnimationFrame(check.raf);
    check.playing = false;
    playBtn.setAttribute('aria-pressed', 'false');
    playBtn.textContent = 'Play masks';
  }
  function togglePlay() {
    if (!stage.classList.contains('no-video')) {
      if (video.paused) video.play().catch(() => {}); else video.pause();
      return;
    }
    if (check.playing) { stopLoop(); return; }
    check.playing = true;
    playBtn.setAttribute('aria-pressed', 'true');
    playBtn.textContent = 'Pause masks';
    const dur = 10;
    check.t0 = performance.now();
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const tick = (now) => {
      const t = ((now - check.t0) / 1000) % dur;
      draw(reduce ? 0 : t);
      if (check.playing && !reduce) check.raf = requestAnimationFrame(tick);
    };
    check.raf = requestAnimationFrame(tick);
  }
  playBtn.addEventListener('click', togglePlay);
  $('#mask-toggle').addEventListener('change', () => draw());

  // ------------------------------------------------------------------ calibration profile + tilt sensor
  // The profile lives only in this browser (localStorage) and goes with every check; see app/calibration.py.
  const PROFILE_KEY = 'bl.profile.v1';
  const CHAIR_DEFAULT = { manual: 65, power: 70 };
  function loadProfile() {
    try { const p = JSON.parse(store.get(PROFILE_KEY) || 'null'); return p && typeof p === 'object' ? p : null; } catch { return null; }
  }
  function saveProfile(p) { store.set(PROFILE_KEY, JSON.stringify(p)); }
  let profile = loadProfile();

  const calForm = $('#calib-form');
  const calStatus = $('#calib-status');
  function calibState() {
    const el = $('#calib-state');
    const mode = profile?.calibration ? 'calibrated' : profile ? 'saved' : 'default';
    el.dataset.mode = mode;
    el.textContent = mode === 'calibrated' ? `Calibrated · ${Math.round(profile.calibration.height_m * 100)} cm`
      : mode === 'saved' ? `Profile · ${Math.round(profile.chair_width_cm)} cm chair` : 'Default assumptions';
  }
  function fillForm() {
    const p = profile || {};
    const type = p.chair_type || 'manual';
    const radio = document.getElementById(`ch-${type}`);
    if (radio) radio.checked = true;
    $('#cal-width').value = p.chair_width_cm ?? CHAIR_DEFAULT[type] ?? 65;
    $('#cal-armrest').value = p.armrest_height_cm ?? 75;
    $('#cal-phone').value = p.phone_model || '';
    $('#cal-focal').value = p.focal_35mm ?? '';
    calibState();
  }
  calForm.querySelectorAll('input[name=chair]').forEach((el) => el.addEventListener('change', () => {
    if (el.checked && CHAIR_DEFAULT[el.value]) $('#cal-width').value = CHAIR_DEFAULT[el.value];
    if (el.checked && el.value === 'custom') $('#cal-width').focus();
  }));
  function formProfile() {
    const num = (sel) => { const v = parseFloat($(sel).value); return Number.isFinite(v) ? v : null; };
    const p = { chair_type: calForm.querySelector('input[name=chair]:checked').value };
    const w = num('#cal-width'); const h = num('#cal-armrest'); const f = num('#cal-focal');
    if (w == null || w < 40 || w > 100) return { error: 'Width must be 40–100 cm.' };
    if (h == null || h < 40 || h > 130) return { error: 'Armrest height must be 40–130 cm.' };
    if (f != null && (f < 10 || f > 80)) return { error: 'Focal length must be 10–80 mm, or left empty.' };
    Object.assign(p, { chair_width_cm: w, armrest_height_cm: h });
    if (f != null) p.focal_35mm = f;
    if ($('#cal-phone').value.trim()) p.phone_model = $('#cal-phone').value.trim().slice(0, 60);
    if (profile?.calibration) p.calibration = profile.calibration;
    return p;
  }
  calForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const p = formProfile();
    if (p.error) { calStatus.textContent = p.error; calStatus.classList.add('warn'); return; }
    profile = p;
    saveProfile(profile);
    calStatus.classList.remove('warn');
    calStatus.textContent = `Saved on this phone: ${Math.round(p.chair_width_cm)} cm chair, phone at about ${Math.round(p.armrest_height_cm)} cm. Sent with every check.`;
    calibState();
  });
  $('#cal-reset').addEventListener('click', () => {
    profile = null;
    try { localStorage.removeItem(PROFILE_KEY); } catch { /* ignore */ }
    fillForm();
    calStatus.classList.remove('warn');
    calStatus.textContent = 'Profile cleared. Checks use default assumptions again.';
  });
  $('#cal-clip').addEventListener('click', () => tilt.mark());
  $('#cal-clip').addEventListener('change', () => { $('#cal-run').disabled = !($('#cal-clip').files || [])[0]; });
  $('#cal-run').addEventListener('click', async () => {
    const f = ($('#cal-clip').files || [])[0];
    if (!f) return;
    const p = formProfile();
    if (p.error) { calStatus.textContent = p.error; calStatus.classList.add('warn'); return; }
    const fd = new FormData();
    fd.append('video', f);
    fd.append('ref_kind', calForm.querySelector('input[name=refkind]:checked').value);
    fd.append('profile', JSON.stringify(p));
    const s = tilt.read(f);
    if (s) fd.append('sensor', JSON.stringify(s));
    $('#cal-run').disabled = true;
    calStatus.classList.remove('warn');
    calStatus.textContent = 'Blurring faces, finding the reference, solving height and tilt…';
    let j = null;
    try {
      const res = await fetch('/api/calibrate', { method: 'POST', body: fd, headers: liveHeaders() });
      j = await res.json().catch(() => null);
      if (!res.ok) throw new Error((j && typeof j.detail === 'string' && j.detail) || `HTTP ${res.status}`);
    } catch (e) {
      calStatus.textContent = `Calibration failed: ${e.message}`;
      calStatus.classList.add('warn');
      $('#cal-run').disabled = false;
      return;
    }
    $('#cal-run').disabled = false;
    if (!j.ok) { calStatus.textContent = j.why; calStatus.classList.add('warn'); return; }
    const c = { ...j.calibration, at: new Date().toISOString().slice(0, 10) };
    profile = { ...p, calibration: c };
    saveProfile(profile);
    calStatus.textContent = `Calibrated: phone at ${Math.round(c.height_m * 100)} cm, tilted ${Math.round(c.pitch_deg)}° down`
      + (c.pitch_bias_deg == null ? ' (no tilt sensor reading, so later checks still estimate tilt).' : `; sensor offset ${c.pitch_bias_deg.toFixed(1)}°.`)
      + ` Solved on ${c.frames_used} frames.`;
    calibState();
  });

  // Tilt: DeviceOrientationEvent beta/gamma, averaged over the window from tapping "film" to the clip arriving.
  // Only a clip recorded just now is paired with a reading; a clip picked from the gallery gets none.
  const tilt = {
    on: false, buf: [], t0: 0,
    listen() {
      if (this.on) return;
      this.on = true;
      window.addEventListener('deviceorientation', (e) => {
        if (e.beta == null || e.gamma == null) return;
        this.buf.push({ t: Date.now(), beta: e.beta, gamma: e.gamma, angle: (screen.orientation && screen.orientation.angle) || window.orientation || 0 });
        if (this.buf.length > 4000) this.buf.splice(0, 2000);
      });
      $('#tilt-btn').setAttribute('aria-pressed', 'true');
      $('#tilt-btn').textContent = 'Phone tilt on';
      $('#tilt-state').textContent = 'Tilt is read while you film and sent with the clip.';
    },
    mark() { this.t0 = Date.now(); },
    read(file) {
      if (!this.on || !file || !this.t0) return null;
      const recent = Date.now() - (file.lastModified || 0) < 120000;
      if (!recent) return null;
      let win = this.buf.filter((s) => s.t >= this.t0);
      if (win.length < 5) win = this.buf.filter((s) => s.t >= this.t0 - 3000 && s.t < this.t0 + 1000);
      if (win.length < 5) return null;
      const avg = (k) => win.reduce((a, s) => a + s[k], 0) / win.length;
      const gammaAbs = win.reduce((a, s) => a + Math.abs(s.gamma), 0) / win.length;
      const angle = win[win.length - 1].angle;
      return { beta: +avg('beta').toFixed(2), gamma: +((angle % 180 ? gammaAbs * Math.sign(avg('gamma') || 1) : avg('gamma'))).toFixed(2), angle, n: win.length };
    },
  };
  if ('DeviceOrientationEvent' in window && window.matchMedia('(pointer: coarse)').matches) {
    $('#tilt-row').hidden = false;
    if (typeof DeviceOrientationEvent.requestPermission !== 'function') tilt.listen();
  }
  $('#tilt-btn').addEventListener('click', async () => {
    try {
      if (typeof DeviceOrientationEvent.requestPermission === 'function') {
        const r = await DeviceOrientationEvent.requestPermission();
        if (r !== 'granted') { $('#tilt-state').textContent = 'Tilt permission was not given; tilt is estimated from the clip.'; return; }
      }
      tilt.listen();
    } catch (e) {
      $('#tilt-state').textContent = `Tilt sensor unavailable (${e.message}); tilt is estimated from the clip.`;
    }
  });
  clip.addEventListener('click', () => tilt.mark());

  function renderAssumptions(result) {
    const a = result.assumptions;
    const el = $('#v-assume');
    if (!a) { el.hidden = true; return; }
    el.dataset.mode = a.mode;
    $('#v-assume-tag').textContent = a.label;
    $('#v-assume-text').textContent = a.main;
    el.hidden = false;
  }
  fillForm();

  // ------------------------------------------------------------------ 1-minute tour
  // Self-running tour for a judge: no upload, no paid calls. It drives the real UI
  // (the sample buttons, setSheet, centreOn) in replay mode. One data array on top:
  // change samples, targets, durations or copy here and the engine follows.
  let tourRunning = false;
  const tourCardEl = $('#tour-card');
  const tourPauseBtn = $('#tour-pause');
  const tourNextBtn = $('#tour-next');
  const tourEndBox = $('#tour-end');
  const tourReduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const tour = { active: false, paused: false, ended: false, idx: 0, remain: 0, seq: 0, swapped: false, timer: 0, calibWasOpen: false };
  const tourOk = (tok) => tour.active && tok === tour.seq;

  function tourMapText() {
    const tow = $('#towing-stat').textContent.trim().replace(/\s+/g, ' ');
    const st = $('#station-line').textContent.trim().replace(/\s+/g, ' ');
    if (/loading|unavailable|could not/i.test(tow)) {
      return 'Gwanghwamun & Gyeongbokgung. Past e-scooter towing reports within the ring (Seoul open data) and both stations\u2019 elevators, live from Seoul Metro.';
    }
    const short = st.length > 150 ? `${st.slice(0, 150)}\u2026` : st;
    return `${tow} Both stations\u2019 elevators are live from Seoul Metro: ${short}`;
  }
  function tourCautiousText() {
    const r = check.result;
    const p = r && r.triage && r.triage.path;
    if (p && p.state && p.evidence && r.verdict && VERDICT_WORD[r.verdict]) {
      return `Muse Spark read \u201c${p.evidence}\u201d \u2014 the path is ${p.state} \u2014 so the answer is ${VERDICT_WORD[r.verdict]} whatever the geometry measured. A model may only make the answer more cautious.`;
    }
    return 'The triage model reads the clip first. A model may only make the answer more cautious, never looser.';
  }
  function tourMeasuredText() {
    const r = check.result;
    if (r && r.gap_cm != null && Array.isArray(r.band_cm)) {
      const chair = Math.round((r.assumptions && r.assumptions.chair_width_cm)
        || (r.measurement_detail && r.measurement_detail.wheelchair_cm) || 65);
      return `With the camera\u2019s height and tilt known, geometry measures the gap from SAM 3.1\u2019s outlines: about ${Math.round(r.gap_cm)} cm, range ${Math.round(r.band_cm[0])}\u2013${Math.round(r.band_cm[1])}, so a ${chair} cm chair fits.`;
    }
    return 'With the camera\u2019s height and tilt known, geometry measures the gap from SAM 3.1\u2019s outlines, with an error range instead of a single number.';
  }
  const TOUR_STEPS = [
    { id: 'map', title: 'Gwanghwamun & Gyeongbokgung', dur: 6,
      targets: [['#towing-stat', '#station-line']],
      text: tourMapText,
      enter: async () => { tourShowOverview(); setSheet('half'); tourCentre(); } },
    { id: 'check', title: 'Film ten seconds', dur: 5,
      targets: [['#clip', '#tilt-row']],
      text: () => 'You film ten seconds at seated height. Faces are blurred on our server before any model sees the clip; the phone\u2019s tilt goes with it.',
      enter: async () => { tourShowCheck(); } },
    { id: 'pipeline', title: 'The pipeline, replayed', dur: 8, sample: 'closed',
      targets: [['#stages']],
      text: () => 'Five stages: blur \u2192 Muse Spark triage \u2192 SAM 3.1 outlines \u2192 geometry measures \u2192 Muse Spark explains. Below is a replay of the recorded run\u2019s stage times.',
      enter: (tok) => tourPlaySample(tok, 'closed', true) },
    { id: 'cautious', title: 'The model can only be more cautious', dur: 9,
      targets: [['#v-saw', '#verdict']],
      text: tourCautiousText },
    { id: 'unknown', title: 'An honest Unknown', dur: 8, sample: 'scooters',
      targets: [['#v-fixit'], ['#verdict']],
      text: () => 'No gap lies straight ahead, so there is nothing honest to measure: Unknown, with how to film again.',
      enter: (tok) => tourPlaySample(tok, 'scooters', false) },
    { id: 'measured', title: 'Measured, with its error', dur: 12, sample: 'known',
      targets: [['#stage']], thenTargets: [['#v-bar'], ['#verdict']], thenAt: 0.5,
      text: tourMeasuredText,
      enter: (tok) => tourPlaySample(tok, 'known', false) },
    { id: 'calib', title: 'Calibrate once', dur: 7,
      targets: [['#calib']],
      text: () => 'Calibrate once: chair width, armrest height, and three seconds of four A4 sheets on the floor. Then your own checks can become decisive.',
      enter: async () => { tourShowCheck(); $('#calib').open = true; } },
  ];
  const TOUR_END_TEXT = 'That was the product, replayed from recorded live runs (Muse Spark 1.3 and SAM 3.1 on the Meta Model API). Live checks on your own clip need the judge token.';

  // Quiet twins of enterCheck/leaveCheck: same panels, no focus moves (the tour owns focus).
  function tourShowCheck() {
    panelOverview.hidden = true;
    panelCheck.hidden = false;
    $('#check-btn').hidden = true;
    crosshair.hidden = false;
    setSheet('half');
  }
  function tourShowOverview() {
    panelCheck.hidden = true;
    panelOverview.hidden = false;
    $('#check-btn').hidden = false;
    crosshair.hidden = true;
  }
  function tourCentre() {
    if (tourReduced.matches) map.setView(CENTRE, 16, { animate: false });
    else centreOn(L.latLng(CENTRE), 16);
  }
  function tourVisible(sel) {
    const el = $(sel);
    return el && !el.hidden && el.getClientRects().length ? el : null;
  }
  function tourHighlight(step, phase) {
    document.querySelectorAll('.tour-hit').forEach((el) => el.classList.remove('tour-hit'));
    const groups = (phase ? step.thenTargets : step.targets) || step.targets;
    const group = (groups || []).find((g) => g.some((sel) => tourVisible(sel)));
    (group || []).forEach((sel) => { const el = tourVisible(sel); if (el) el.classList.add('tour-hit'); });
  }
  function tourScrollFirst() {
    const hit = document.querySelector('.tour-hit');
    if (hit) hit.scrollIntoView({ block: 'center', behavior: tourReduced.matches ? 'auto' : 'smooth' });
  }
  function tourRender(step) {
    $('#tour-title').textContent = step.title;
    $('#tour-text').textContent = step.text();
    $('#tour-progress').textContent = `${tour.idx + 1} / ${TOUR_STEPS.length}`;
  }
  function tourWaitResult(tok, prev, timeoutMs) {
    return new Promise((resolve) => {
      const t0 = performance.now();
      const tick = () => {
        if (!tourOk(tok)) { resolve(null); return; }
        if (check.result !== prev) { resolve(check.result); return; }
        if (performance.now() - t0 > timeoutMs) { resolve(null); return; }
        setTimeout(tick, 150);
      };
      tick();
    });
  }
  async function tourPlaySample(tok, sampleKey, replay) {
    tourShowCheck();
    const btn = document.querySelector(`button[data-sample="${sampleKey}"]`);
    const prev = check.result;
    if (btn && !btn.disabled) btn.click();
    const result = await tourWaitResult(tok, prev, 9000);
    if (!tourOk(tok)) return;
    const step = TOUR_STEPS[tour.idx];
    $('#tour-text').textContent = step.text();
    if (replay) { $('#stages').hidden = false; stageReset(); }  // visible before the highlight and scroll target it
    tourHighlight(step, false);
    tourScrollFirst();
    if (replay) await tourReplayStages(tok, result);
  }
  async function tourReplayStages(tok, result) {
    const ol = $('#stages');
    ol.hidden = false;
    ol.setAttribute('aria-label', 'Replay of the recorded run\u2019s stage times');
    stageReset();
    const order = ['blur', 'triage', 'sam', 'measure', 'explain'];
    const keys = { blur: 'prepare_s', triage: 'triage_s', sam: 'sam_s', measure: 'measure_s', explain: 'explain_s' };
    const rec = (result && result.timings) || {};
    const w = order.map((k) => Math.max(0.05, Number(rec[keys[k]]) || 0.6));
    const sum = w.reduce((a, b) => a + b, 0);
    const budget = Math.max(0, tour.remain * 1000 - 600);
    for (let i = 0; i < order.length; i += 1) {
      if (!tourOk(tok)) return;
      const li = ol.querySelector(`li[data-stage="${order[i]}"]`);
      if (li) li.dataset.state = 'run';
      let wait = budget > 0 ? (w[i] / sum) * budget : 0;
      while (wait > 0) {
        if (!tourOk(tok)) return;
        if (!tour.paused) wait -= 100;
        await new Promise((r) => setTimeout(r, 100));
      }
      if (!tourOk(tok)) return;
      stageEvent({ stage: order[i], status: 'done', s: rec[keys[order[i]]] ?? '' });
    }
  }
  async function tourEnter(i) {
    const tok = ++tour.seq;
    tour.idx = i;
    tour.swapped = false;
    const step = TOUR_STEPS[i];
    tour.remain = step.dur;
    tourRender(step);
    tourCardEl.hidden = false;
    if (step.enter) await step.enter(tok);
    if (!tourOk(tok)) return;
    $('#tour-text').textContent = step.text();
    tourHighlight(step, false);
    tourScrollFirst();
  }
  function tourNext() {
    if (!tour.active || tour.ended) return;
    if (tour.idx + 1 >= TOUR_STEPS.length) tourEnd();
    else tourEnter(tour.idx + 1);
  }
  function tourTick() {
    if (!tour.active || tour.paused || tour.ended) return;
    const step = TOUR_STEPS[tour.idx];
    tour.remain -= 0.2;
    if (step.thenTargets && !tour.swapped && (step.dur - tour.remain) >= step.dur * (step.thenAt || 0.5)) {
      tour.swapped = true;
      tourHighlight(step, true);
      tourScrollFirst();
    }
    if (tour.remain <= 0) tourNext();
  }
  function tourSetPaused(p) {
    tour.paused = p;
    tourPauseBtn.setAttribute('aria-pressed', String(p));
    tourPauseBtn.textContent = p ? '▶ Resume' : '⏸ Pause';
  }
  function startTour() {
    if (!tour.active) tour.calibWasOpen = $('#calib').open;
    tourRunning = true;
    tour.active = true;
    tour.paused = false;
    tour.ended = false;
    tourCardEl.dataset.mode = 'steps';
    tourCardEl.hidden = false;
    tourPauseBtn.hidden = false;
    tourNextBtn.hidden = false;
    tourEndBox.hidden = true;
    tourSetPaused(false);
    tourCardEl.focus();
    if (!tour.timer) tour.timer = setInterval(tourTick, 200);
    tourEnter(0);
  }
  function tourEnd() {
    tour.ended = true;
    tour.seq += 1;  // cancel any pending sample wait or stage replay
    document.querySelectorAll('.tour-hit').forEach((el) => el.classList.remove('tour-hit'));
    $('#tour-title').textContent = 'That was the product';
    $('#tour-text').textContent = TOUR_END_TEXT;
    $('#tour-progress').textContent = `${TOUR_STEPS.length} / ${TOUR_STEPS.length}`;
    tourPauseBtn.hidden = true;
    tourNextBtn.hidden = true;
    tourEndBox.hidden = false;
  }
  function tourCloseFull() {
    tour.seq += 1;
    tour.active = false;
    tour.paused = false;
    tour.ended = false;
    tourRunning = false;
    tourCardEl.hidden = true;
    document.querySelectorAll('.tour-hit').forEach((el) => el.classList.remove('tour-hit'));
    $('#stages').setAttribute('aria-label', 'Live check progress');
    $('#calib').open = tour.calibWasOpen;
    tourShowOverview();
    setSheet('peek');
    tourPauseBtn.hidden = false;
    tourNextBtn.hidden = false;
    tourEndBox.hidden = true;
  }
  function tourCloseStay() {
    tour.seq += 1;
    tour.active = false;
    tour.paused = false;
    tour.ended = false;
    tourRunning = false;
    tourCardEl.hidden = true;
    document.querySelectorAll('.tour-hit').forEach((el) => el.classList.remove('tour-hit'));
    $('#stages').setAttribute('aria-label', 'Live check progress');
    tourShowCheck();
    $('#clip').focus();
  }
  $('#tour-btn').addEventListener('click', startTour);
  tourPauseBtn.addEventListener('click', () => tourSetPaused(!tour.paused));
  tourNextBtn.addEventListener('click', tourNext);
  $('#tour-close').addEventListener('click', tourCloseFull);
  $('#tour-replay').addEventListener('click', startTour);
  $('#tour-try').addEventListener('click', tourCloseStay);
  document.addEventListener('click', (e) => {  // a click elsewhere pauses, so a judge can explore
    if (!tour.active || tour.paused || tour.ended || !e.isTrusted) return;
    if (tourCardEl.contains(e.target)) return;
    tourSetPaused(true);
  }, true);
  document.addEventListener('keydown', (e) => {
    if (!tour.active) return;
    if (e.key === 'Escape') { e.preventDefault(); tourCloseFull(); return; }
    if (tour.ended) return;
    if (e.target.tagName === 'BUTTON' && (e.key === ' ' || e.key === 'Enter')) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.key === ' ' && e.target.tagName !== 'BUTTON') { e.preventDefault(); tourSetPaused(!tour.paused); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); tourNext(); }
  });
  function maybeTour(boot) {
    if (location.hash !== '#tour') return;
    Promise.race([boot, new Promise((r) => setTimeout(r, 2000))]).then(() => startTour());
  }

  // ------------------------------------------------------------------ start
  layout();
  centreOn(L.latLng(CENTRE), 16);
  const bootData = Promise.allSettled([loadTowing(500), loadStation()]);
  if (params.get('check') === '1' || params.get('sample')) enterCheck();
  if (params.get('sample')) runSample(false, crosshairLatLng());
  if (params.get('open') === '1') setSheet('half');
  maybeTour(bootData);
})();

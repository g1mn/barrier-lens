/* Barrier Lens front end: map of Gwanghwamun & Gyeongbokgung + "Check a spot" flow. No build step. */
(async () => {
  'use strict';

  // _() translates text this page wrote, tr() text the server or a model wrote (static/i18n.js).
  const { t: _, tr, num: nf, lang } = window.I18N;
  await window.I18N.ready;
  window.I18N.apply();
  const $ = (sel) => document.querySelector(sel);
  const CENTRE = [37.5759283, 126.9768176];  // Gwanghwamun Gate; the map opens here
  const params = new URLSearchParams(location.search);
  const VERDICT_WORD = { passes: _('Passes'), tight: _('Tight'), blocked: _('Blocked'), unknown: _('Unknown') };
  const PATH_STATE = { open: _('open'), narrowed: _('narrowed'), closed: _('closed') };
  const MASK_COLORS = ['#ffd23f', '#4cc9f0', '#ff6bd6', '#7ee081', '#ff8c42'];

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const monthLabel = (d) => (d ? new Date(Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, 1))
    .toLocaleDateString(lang, { year: 'numeric', month: 'short', timeZone: 'UTC' }) : '');
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
    themeBtn.setAttribute('aria-label', dark ? _('Dark theme on. Switch to light theme') : _('Light theme on. Switch to dark theme'));
  }
  applyTheme(store.get('bl-theme') === 'light' ? 'light' : 'dark');
  themeBtn.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    store.set('bl-theme', next);
  });

  // ------------------------------------------------------------------ map
  const map = L.map('map', { zoomControl: false, attributionControl: true, maxZoom: 19 }).setView(CENTRE, 16);
  L.control.zoom({ position: 'topright', zoomInTitle: _('Zoom in'), zoomOutTitle: _('Zoom out') }).addTo(map);
  // Base map: OpenFreeMap vector tiles (OpenStreetMap data) drawn by MapLibre, with place names in the page's
  // language: name:<lang>, then English or the Latin spelling, then the local (Korean) name. The intro's globe
  // uses the same style, so it still lands on exactly this view. Without MapLibre or WebGL: OSM raster tiles.
  const NAME_KEYS = { en: ['name:en', 'name:latin'], ko: ['name:ko'], ja: ['name:ja', 'name:latin'],
    'zh-CN': ['name:zh-Hans', 'name:zh', 'name:latin'], 'zh-TW': ['name:zh-Hant', 'name:zh', 'name:latin'] };
  const nameField = ['coalesce', ...(NAME_KEYS[lang] || [`name:${lang}`, 'name:en', 'name:latin']).map((k) => ['get', k]), ['get', 'name']];
  const baseStyle = fetch('https://tiles.openfreemap.org/styles/liberty')
    .then((r) => { if (!r.ok) throw new Error(`style HTTP ${r.status}`); return r.json(); })
    .then((st) => {
      st.layers.forEach((l) => {
        const f = l.layout && l.layout['text-field'];
        if (f && JSON.stringify(f).includes('"name')) l.layout['text-field'] = nameField;
      });
      return st;
    });
  const tileUrl = baseStyle.then((s) => fetch(s.sources.openmaptiles.url)).then((r) => r.json()).then((j) => j.tiles[0]);
  tileUrl.catch(() => {});  // only the intro's dive uses it
  baseStyle.then((style) => {
    if (!L.maplibreGL) throw new Error('no MapLibre');
    L.maplibreGL({ style }).addTo(map);
  }).catch(() => {
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
  });
  // keep: replaces Leaflet's default flag-emoji prefix with a plain Leaflet link.
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  map.attributionControl.addAttribution(_('Towing, elevators: {link} (KOGL Type 1)', { link: '<a href="https://data.seoul.go.kr/">Seoul Open Data</a>' }));

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
  // A longer tour label (Visite, ツアー, Tham quan) can push the name or tagline onto two lines: drop the label.
  function fitBar() {
    const bar = $('.bar');
    bar.classList.remove('tight');
    const wraps = (el) => el.offsetHeight > parseFloat(getComputedStyle(el).lineHeight) * 1.5;
    if (wraps($('.bar h1')) || wraps($('.tagline'))) bar.classList.add('tight');
  }
  function layout() {
    fitBar();
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
    $('#sheet-handle-label').textContent = open ? _('Collapse panel') : _('Expand panel');
    requestAnimationFrame(layout);
  }
  handle.addEventListener('click', () => setSheet(sheet.dataset.state === 'peek' ? 'half' : 'peek'));
  window.addEventListener('resize', layout);
  if ('ResizeObserver' in window) new ResizeObserver(() => layout()).observe(sheet);

  // ------------------------------------------------------------------ towing history
  let towingLoaded = false;
  async function loadTowing(r) {
    const stat = $('#towing-stat');
    stat.textContent = _('Loading towing history…');
    let d;
    try {
      const res = await fetch(`/api/towing?lat=${CENTRE[0]}&lon=${CENTRE[1]}&r=${r}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      d = await res.json();
    } catch (e) {
      stat.textContent = _('Could not load towing history ({error}).', { error: e.message });
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
        ? _('Towing history is not cached yet (run scripts/fetch_seoul.py).')
        : _('No towing reports in the cached data within {r} m.', { r });
    } else {
      const first = pts.reduce((a, p) => (p.first < a ? p.first : a), pts[0].first);
      const last = pts.reduce((a, p) => (p.last > a ? p.last : a), pts[0].last);
      towingLoaded = true;
      stat.innerHTML = `${_('{n} e-scooter towing reports at {addresses} addresses within {distance} of Gwanghwamun Gate', {
        n: `<strong>${nf(d.records)}</strong>`, addresses: nf(pts.length), distance: r >= 1000 ? `${r / 1000} km` : `${r} m` })} · ${esc(monthLabel(first))} – ${esc(monthLabel(last))}`;
    }

    const list = $('#hotspots');
    list.innerHTML = '';
    [...pts].sort((a, b) => b.count - a.count).slice(0, 5).forEach((p) => {
      const li = document.createElement('li');
      li.innerHTML = `<span class="addr" lang="ko">${esc(shortAddr(p.addr))}<span class="meta" lang="${lang}">${esc(_('within {m} m of Gwanghwamun Gate · last {month}', { m: p.band_m, month: monthLabel(p.last) }))}</span></span>
        <span class="n">${nf(p.count)}<span class="visually-hidden"> ${esc(_('reports'))}</span></span>`;
      list.appendChild(li);
    });

    const cav = $('#caveats');
    cav.innerHTML = '';
    [...(cov.caveats || []).map(tr), cov.source ? _('Source: {source}.', { source: cov.source }) : null].filter(Boolean).forEach((t) => {
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
      line.textContent = _('Exit elevator status unavailable ({error}).', { error: e.message });
      return;
    }
    exitLayer.clearLayers();
    const LIFT = { working: _('working'), 'out of service': _('out of service'), 'none listed': _('none listed') };
    (d.exits || []).forEach((x) => {
      const cls = x.elevator === 'working' ? 'ev-working' : x.elevator === 'out of service' ? 'ev-out' : '';
      const icon = L.divIcon({ className: '', html: `<div class="exit-pin ${cls}">${esc(x.ref)}</div>`, iconSize: [30, 30], iconAnchor: [15, 15] });
      const rows = (x.elevator_rows || []).map((r) => `<li lang="ko">${esc(r.name.replace(/^승강기\)/, ''))}: <strong>${esc(r.status)}</strong></li>`).join('');
      const lift = LIFT[x.elevator] || x.elevator;
      const body = x.elevator === 'none listed'
        ? `<p>${esc(_('No elevator is listed for this exit in the Seoul Metro feed.'))}</p>`
        : `<p>${_('Elevator {state}', { state: `<strong>${esc(lift)}</strong>` })}</p><ul>${rows}</ul>`;
      const label = _('{station} exit {ref}', { station: tr(x.station_name), ref: x.ref });
      L.marker([x.lat, x.lon], { icon, title: _('{exit}: elevator {state}', { exit: label, state: lift }), keyboard: true, zIndexOffset: cls ? 500 : 0 })
        .bindPopup(`<h4>${esc(label)}</h4>${body}<p class="ko">${esc(_('Status {source} · {time}', { source: d.status_source, time: fmtTime(d.status_as_of) }))}</p>`)
        .addTo(exitLayer);
    });
    const parts = (d.stations || []).map((s) => {
      const withLift = (s.exits || []).filter((x) => x.elevator === 'working' || x.elevator === 'out of service');
      const bit = !withLift.length ? esc(_('no exit elevator listed'))
        : withLift.map((x) => (x.elevator === 'working'
          ? esc(_('exit {ref} elevator working', { ref: x.ref }))
          : `<strong>${esc(_('exit {ref} out of service', { ref: x.ref }))}</strong>`)).join(', ');
      return `${esc(tr(s.name))} (${esc(tr(s.line))}): ${bit}`;
    });
    line.innerHTML = `${parts.join(' · ')} <span class="asof">(${esc(d.status_source)}, ${esc(fmtTime(d.status_as_of))})</span>`;
  }
  function fmtTime(iso) {
    if (!iso) return _('time unknown');
    const t = new Date(iso.replace(/([+-]\d{2})(\d{2})$/, '$1:$2'));
    if (Number.isNaN(t.getTime())) return iso;
    return t.toLocaleString(lang === 'en' ? 'en-GB' : lang, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Seoul' }) + ' KST';
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
    setStatus(_('Clip ready: {name} ({size} MB). Place the pin, then measure.', { name: f.name, size: nf(f.size / 1e6, { maximumFractionDigits: 1 }) }));
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
    fd.append('lang', lang);  // Muse Spark writes its explanation in the page's language
    if (profile) fd.append('profile', JSON.stringify(profile));
    const s = tilt.read(f);
    if (s) fd.append('sensor', JSON.stringify(s));
    $('#analyze-btn').disabled = true;
    setStatus(_('Measuring… this takes about a minute. Each step below shows when it finishes.'));
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
    blur: (e) => (e.faces == null ? '' : e.faces === 1 ? _('1 face detection blurred in {frames} frames', { frames: e.frames })
      : _('{n} face detections blurred in {frames} frames', { n: e.faces, frames: e.frames })),
    triage: (e) => [e.usable === false ? _('clip not usable') : '', e.path ? _('path {state}', { state: PATH_STATE[e.path] || e.path }) : '',
      (e.barriers || []).map(tr).join(', ')].filter(Boolean).join(' · '),
    sam: (e) => Object.entries(e.outlined || {}).map(([c, n]) => _('{concept} {n} frames', { concept: tr(c), n })).join(', '),
    measure: (e) => [VERDICT_WORD[e.verdict] || e.verdict, tr(e.mode)].filter(Boolean).join(' · '),
    explain: (e) => (e.model === 'template' ? _('fixed sentence (model text failed a check)') : ''),
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
      setStatus(_('Could not reach the server ({error}).', { error: e.message }), true);
      return null;
    }
    if (!res.ok || !res.body) {
      let detail = '';
      try { detail = (await res.json()).detail; } catch { /* not JSON */ }
      setStatus(_('Could not measure: {reason}', { reason: typeof detail === 'string' && detail ? tr(detail) : `HTTP ${res.status}` }), true);
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
        else if (ev.error) { setStatus(_('Could not measure: {reason}', { reason: tr(ev.error) }), true); return null; }
      }
    }
    if (!result) setStatus(_('The check ended without a result. Please try again.'), true);
    return result;
  }

  // GET/POST JSON; on failure, says why in the status line and returns null.
  async function getJson(url, opts) {
    let res;
    try {
      res = await fetch(url, opts);
    } catch (e) {
      setStatus(_('Could not reach the server ({error}).', { error: e.message }), true);
      return null;
    }
    if (res.ok) return res.json();
    let detail = '';
    try { detail = (await res.json()).detail; } catch { /* not JSON */ }
    setStatus(_('Could not measure: {reason}', { reason: typeof detail === 'string' && detail ? tr(detail) : `HTTP ${res.status}` }), true);
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
    return (r.recorded && tr(r.recorded.about)) || _('Sample clip filmed elsewhere — the pin position is illustrative');
  }
  function statusLine(r) {
    const b = r.blur || {};
    const faces = b.faces == null ? '' : ` ${_('Face detections blurred before upload: {faces} in {with} of {frames} frames.', { faces: b.faces, with: b.frames_with_faces, frames: b.frames })}`;
    const usd = `$${Number((r.cost && r.cost.usd_estimate) || 0).toFixed(2)}`;
    const cost = !r.cost ? '' : (r.recorded && r.cost.sam_frames === 0)
      ? ` ${_('SAM masks reused from an earlier live call of this clip (not billed again).')}`
      : ` ${r.recorded ? _('SAM frames billed when it was recorded: {n} ({usd}).', { n: r.cost.sam_frames, usd })
        : _('Paid SAM frames this check: {n} ({usd}).', { n: r.cost.sam_frames, usd })}`;
    const head = r.recorded ? `${r.recorded.about ? `${tr(r.recorded.about)} ` : ''}${_('Recorded live run ({triage} and {sam} on the Meta Model API, {date}), replayed with no model calls now.', { triage: r.models.triage, sam: r.models.sam, date: r.recorded.at.slice(0, 10) })}`
      : r.sample ? _('Sample clip, replayed results (no live model calls).') : _('Done.');
    return `${head}${faces}${cost}`;
  }

  const sampleBtns = [$('#sample-btn'), $('#sample-closed-btn'), $('#sample-known-btn')];
  sampleBtns.forEach((btn) => btn.addEventListener('click', async () => {
    const at = crosshairLatLng();
    if (params.get('verdict')) { await runSample(false, at); return; }  // hand-made UI fixture variants
    sampleBtns.forEach((x) => { x.disabled = true; });
    setStatus(_('Loading the sample clip…'));
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
    setStatus(statusLine(result) + (result.precomputed ? ` ${_('The clip itself is not on this server, so only the masks are shown.')}` : ''));
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
    $('#confirm-btn').textContent = _('Pin confirmed');
    setStatus(_('Pin confirmed on this screen. Sharing pins with others is not built yet.'));
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
    if (!keepVideo) setStatus(_('Sample: hand-made masks and numbers, not a measurement.'));
  }

  function showResult(result, sample, at) {
    check.result = result;
    check.sample = sample;
    check.maskCache.clear();
    const tracks = toTracks(result);
    const shown = tracks.filter((t) => t.frames.length);
    $('#mask-legend').innerHTML = (sample ? `<span class="sample-tag">${esc(_('Sample'))}</span>` : '')
      + (shown.length ? shown.map((t) => `<span><i style="background:${t.color}"></i>${esc(tr(t.concept))}${t.role === 'reference' ? ` ${esc(_('(scale reference)'))}` : ''}</span>`).join('')
        : `<span>${esc(_('No masks found'))}</span>`);
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
    if (b[1] > BAR_MAX_CM) return _('{lo} cm – over {m} m', { lo: Math.round(b[0]), m: BAR_MAX_CM / 100 });
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
    $('#v-bar-cap').innerHTML = `<span class="k band">${esc(_('Measured range {band}', { band: bandText(band) }))}</span>`
      + `<span class="k chair">${esc(_('Your chair {n} cm', { n: Math.round(chair) }))}</span><span class="k need">${esc(_('Needed with room {n} cm', { n: Math.round(need) }))}</span>`;
    fig.hidden = false;
  }
  function renderNotes(r) {
    const notes = (r.notes || []).filter(Boolean);
    const items = notes.map((n) => `<li>${esc(tr(n))}</li>`);
    if (notes.some((n) => n.includes('CC BY'))) items.push(`<li><a href="/static/demo/ATTRIBUTION.txt">${esc(_('SANPO dataset (Google Research), CC BY 4.0 — modified; full credits'))}</a></li>`);
    const ul = $('#v-notes');
    ul.innerHTML = items.join('');
    ul.hidden = !items.length;
  }
  // What Muse Spark saw (path state, evidence) next to what SAM 3.1 outlined; says so when they disagree.
  const PATH_WORD = { open: _('Path looks open'), narrowed: _('Path is narrowed'), closed: _('Path is closed') };
  function renderSaw(r) {
    const tri = r.triage || {};
    const sec = $('#v-saw');
    if (r.handmade || tri.usable == null) { sec.hidden = true; return; }
    const p = tri.path || {};
    $('#v-path').textContent = p.state ? `${PATH_WORD[p.state] || p.state} — ${tr(p.evidence || '')}` : '';
    const sam = Object.fromEntries(toTracks(r).map((t) => [t.concept, t.frames.length]));
    const rows = (tri.barriers || []).map((b) => {
      const n = sam[b.concept];
      const outl = n ? _('SAM 3.1 outlined it in {n} frames', { n }) : (n === 0 ? _('SAM 3.1 found no outline for it, so it is not measured') : '');
      return `<li><b>${esc(tr(b.concept))}</b> — ${esc(tr(b.evidence))}${outl ? ` <span class="${n ? 'ok' : 'miss'}">${esc(outl)}</span>` : ''}</li>`;
    });
    $('#v-saw-list').innerHTML = rows.join('');
    sec.hidden = !(rows.length || p.state);
  }


  // An "unknown" that calibration can fix gets a button for it: the tilt sensor, or the one-time calibration.
  const FIX_FOR = {
    tilt: [_('Turn on phone tilt'), _('The tilt was not recorded. With the sensor on, the next clip carries it.'), 'tilt'],
    default_pass: [_('Calibrate now'), _('Nothing confirmed the scale. Tilt or a one-time calibration does.'), 'calib'],
    height: [_('Calibrate now'), _('Your armrest height sets how high the phone is.'), 'calib'],
    'field of view': [_('Calibrate now'), _('Your camera’s angle of view is not known yet.'), 'calib'],
    roll: [_('How to hold the phone'), _('Hold it level, not tipped to one side.'), 'calib'],
  };
  function renderFixit(r, verdict) {
    const p = $('#v-fixit');
    const d = r.measurement_detail || {};
    let fix = verdict === 'unknown' && !r.handmade && FIX_FOR[d.capped || d.dominant_error];
    if (fix && fix[2] === 'tilt' && $('#tilt-row').hidden) {  // no tilt sensor on this device
      fix = [_('Calibrate now'), _('The tilt was not recorded. A one-time calibration with four A4 sheets on the ground tells us your usual tilt.'), 'calib'];
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
    kicker.textContent = result.handmade ? _('Sample · hand-made, not a measurement')
      : sample ? _('Sample clip, filmed elsewhere · replayed model results') : _('Measured from your clip');
    kicker.classList.toggle('sample', sample);
    // unknown: the middle of a too-wide range is not an answer, so it is shown as a best guess, not a headline
    $('#v-nums').classList.toggle('weak', verdict === 'unknown');
    $('#v-nums').hidden = result.gap_cm == null && !Array.isArray(result.band_cm);  // e.g. blocked because the path is closed
    $('#v-gap-label').textContent = verdict === 'unknown' ? _('Best guess (not an answer)') : _('Measured gap');
    $('#v-gap').textContent = result.gap_cm == null ? '—' : `${Math.round(result.gap_cm)} cm`;
    $('#v-band').textContent = Array.isArray(result.band_cm) ? bandText(result.band_cm) : '—';
    renderAssumptions(result);
    renderBar(result);
    renderFixit(result, verdict);
    renderNotes(result);
    renderSaw(result);
    $('#v-why').textContent = (result.why || '').replace(/\(default \((.*?)\)\)/g, '($1)');
    $('#v-why-lang').hidden = lang === 'en';  // the technical trace stays in English
    $('#v-explain').textContent = tr(typeof result.explanation === 'string' ? result.explanation : (result.explanation?.text || ''));
    const md = result.models || {};
    const srcs = [...new Set(toTracks(result).map((t) => t.source).filter(Boolean))].join(', ');
    const triTxt = tri && tri.usable == null ? 'triage skipped' : `triage ${md.triage || 'unknown'}`;
    $('#v-model').textContent = result.handmade
      ? 'Masks and text are a hand-made fixture for testing the screen.'
      : `Masks ${md.sam || 'SAM'}${srcs ? ` (${srcs})` : ''} · ${triTxt} · text ${md.explain || 'none'} · width from geometry, not from a model.`;
    $('#confirm-btn').disabled = false;
    $('#confirm-btn').textContent = _('Confirm this pin');
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
    const marker = L.marker(at, { icon: pinIcon(verdict, false), draggable: true, keyboard: true, title: _('Checked spot: {verdict}', { verdict: VERDICT_WORD[verdict] }), zIndexOffset: 1000 })
      .bindPopup(() => `<h4>${esc(VERDICT_WORD[verdict])}${check.sample ? ` ${esc(_('(sample)'))}` : ''}</h4>${origin ? `<p>${esc(origin)}</p>` : ''}<p>${esc(m.gap_cm == null ? _('No width measured') : _('Gap {n} cm', { n: Math.round(m.gap_cm) }))}${Array.isArray(m.band_cm) ? ` (${Math.round(m.band_cm[0])}–${Math.round(m.band_cm[1])} cm)` : ''}</p><p class="ko">${esc(check.pin && check.pin.confirmed ? _('Confirmed') : _('Not confirmed yet. Drag to adjust.'))}</p>`)
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
    playBtn.textContent = _('Play masks');
  }
  function togglePlay() {
    if (!stage.classList.contains('no-video')) {
      if (video.paused) video.play().catch(() => {}); else video.pause();
      return;
    }
    if (check.playing) { stopLoop(); return; }
    check.playing = true;
    playBtn.setAttribute('aria-pressed', 'true');
    playBtn.textContent = _('Pause masks');
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
    el.textContent = mode === 'calibrated' ? _('Calibrated · {n} cm', { n: Math.round(profile.calibration.height_m * 100) })
      : mode === 'saved' ? _('Profile · {n} cm chair', { n: Math.round(profile.chair_width_cm) }) : _('Default assumptions');
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
    if (w == null || w < 40 || w > 100) return { error: _('Width must be 40–100 cm.') };
    if (h == null || h < 40 || h > 130) return { error: _('Armrest height must be 40–130 cm.') };
    if (f != null && (f < 10 || f > 80)) return { error: _('Focal length must be 10–80 mm, or left empty.') };
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
    calStatus.textContent = _('Saved on this phone: {w} cm chair, phone at about {h} cm. Sent with every check.', { w: Math.round(p.chair_width_cm), h: Math.round(p.armrest_height_cm) });
    calibState();
  });
  $('#cal-reset').addEventListener('click', () => {
    profile = null;
    try { localStorage.removeItem(PROFILE_KEY); } catch { /* ignore */ }
    fillForm();
    calStatus.classList.remove('warn');
    calStatus.textContent = _('Profile cleared. Checks use default assumptions again.');
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
    calStatus.textContent = _('Blurring faces, finding the reference, solving height and tilt…');
    let j = null;
    try {
      const res = await fetch('/api/calibrate', { method: 'POST', body: fd, headers: liveHeaders() });
      j = await res.json().catch(() => null);
      if (!res.ok) throw new Error((j && typeof j.detail === 'string' && j.detail) || `HTTP ${res.status}`);
    } catch (e) {
      calStatus.textContent = _('Calibration failed: {error}', { error: tr(e.message) });
      calStatus.classList.add('warn');
      $('#cal-run').disabled = false;
      return;
    }
    $('#cal-run').disabled = false;
    if (!j.ok) { calStatus.textContent = tr(j.why); calStatus.classList.add('warn'); return; }
    const c = { ...j.calibration, at: new Date().toISOString().slice(0, 10) };
    profile = { ...p, calibration: c };
    saveProfile(profile);
    calStatus.textContent = [
      _('Calibrated: phone at {h} cm, tilted {p}° down.', { h: Math.round(c.height_m * 100), p: Math.round(c.pitch_deg) }),
      c.pitch_bias_deg == null ? _('No tilt sensor reading, so later checks still estimate tilt.')
        : _('Sensor offset {b}°.', { b: nf(c.pitch_bias_deg, { maximumFractionDigits: 1, minimumFractionDigits: 1 }) }),
      _('Solved on {n} frames.', { n: c.frames_used }),
    ].join(' ');
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
      $('#tilt-btn').textContent = _('Phone tilt on');
      $('#tilt-state').textContent = _('Tilt is read while you film and sent with the clip.');
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
        if (r !== 'granted') { $('#tilt-state').textContent = _('Tilt permission was not given; tilt is estimated from the clip.'); return; }
      }
      tilt.listen();
    } catch (e) {
      $('#tilt-state').textContent = _('Tilt sensor unavailable ({error}); tilt is estimated from the clip.', { error: e.message });
    }
  });
  clip.addEventListener('click', () => tilt.mark());

  function renderAssumptions(result) {
    const a = result.assumptions;
    const el = $('#v-assume');
    if (!a) { el.hidden = true; return; }
    el.dataset.mode = a.mode;
    $('#v-assume-tag').textContent = tr(a.label);
    $('#v-assume-text').textContent = tr(a.main);
    el.hidden = false;
  }
  fillForm();

  // ------------------------------------------------- opening intro (~13 s)
  // One clock drives everything: render(t) is a pure function of t, so ?intro_at=9.5 freezes any moment
  // (add &go to play on from there). A MapLibre globe dives from space to Gwanghwamun Gate and lands on
  // exactly the Leaflet view under it; the towing rings and station exits come up on the real map; then one
  // recorded check plays in a viewfinder (blur -> Muse Spark -> SAM 3.1 -> geometry -> Muse Spark) and the
  // page's own panel slides in. Every load unless the header toggle turned it off; skip with the button,
  // Esc or a tap. #tour plays it first, #nointro never. Without WebGL or MapLibre the globe is left out and
  // the rest still plays.
  const INTRO_PREF = 'bl-intro';  // 'off' once the header toggle turns the intro off
  let introDone = Promise.resolve();
  let playIntroNow = () => Promise.resolve();
  {
    const overlay = $('#intro');
    const skipBtn = $('#intro-skip');
    const globeHost = $('#intro-globe');
    const fx = $('#intro-fx');
    const heads = $('#iv-heads');
    const crumbs = $('#iv-crumbs');
    const cam = $('#iv-cam');
    const screen = $('#iv-screen');
    const zoomBox = $('#iv-zoom');
    const video = $('#iv-video');
    const still = $('#iv-still');
    const camFx = $('#iv-fx');
    const reduceMq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const AT = params.has('intro_at') ? Number(params.get('intro_at')) || 0 : null;
    // Timeline in seconds. Stage i of the rail runs from RAIL[i] to RAIL[i + 1].
    const T = {
      dive: 5.4, swap: [5.4, 5.75], about: 2.3,
      rings: 5.6, ringDur: 1.0, ringGap: 0.22, exits: 6.35,
      cam: 7.3, play: 7.55, blur: 7.7, spark: [8.95, 10.4], freeze: 10.55,
      sam: [10.65, 11.7], measure: [11.7, 12.95], verdict: 12.95, explain: [13.2, 14.15],
      handoff: 14.4, end: 15.0,
    };
    // zoom over the dive (seconds, zoom): the globe turns to East Asia, South Korea fills, Seoul, then a slow
    // landing. A monotone cubic through these keys keeps the speed continuous; the centre follows the zoom
    // along the van Wijk curve, so pan and zoom never stop and restart.
    let skipOnce = false;  // a language switch reloads the page; the intro does not replay for it
    try { skipOnce = sessionStorage.getItem('bl-intro-skip-once') === '1'; sessionStorage.removeItem('bl-intro-skip-once'); } catch { /* private mode */ }
    const ZKEYS = [[0, 0], [1.7, 2.6], [3.2, 5.4], [4.1, 9.8], [T.dive, 15]];  // first and last zoom set at start
    const RAIL = [T.blur, T.spark[0], T.sam[0], T.measure[0], T.verdict, T.explain[1]];
    // what the product does, one line at a time during the dive (icons: ruler, error bar, shield, globe)
    const FEATS = [
      ['<path d="M3 17 17 3l4 4L7 21zM7 13l2 2m1-5 2 2m1-5 2 2"/>', _('Measured against your own wheelchair')],
      ['<path d="M4 12h16M4 8v8m16-8v8m-8-6v4"/>', _('Every answer shows its error')],
      ['<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6zm-3 9 2 2 4-4"/>', _('No model ever guesses a distance')],
      ['<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18m0-18c-3 3-3 15 0 18"/>', _('In ten languages')],
    ];
    const GATE = [CENTRE[1], CENTRE[0]];
    const SEOUL = [126.9780, 37.5665];
    const KOREA_TAG = [129.6, 35.7];  // off the south-east coast, clear of the Seoul label
    const START = [8, 18];  // the globe opens on Europe and Africa and turns east
    const clamp01 = (x) => Math.min(1, Math.max(0, x));
    const seg = (t, a, b) => clamp01((t - a) / (b - a));
    const fadeIO = (t, a, b, f = 0.3) => seg(t, a, a + f) * (1 - seg(t, b - f, b));
    const inOut = (u) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2);
    const outCubic = (u) => 1 - Math.pow(1 - u, 3);
    const outBack = (u) => (u <= 0 ? 0 : 1 + 2.7 * Math.pow(u - 1, 3) + 1.7 * Math.pow(u - 1, 2));
    const num = (n) => nf(Math.round(n));
    const cssVar = (n) => (getComputedStyle(document.documentElement).getPropertyValue(n) || '').trim();
    let cancelPlay = null;

    if (skipBtn) skipBtn.addEventListener('click', (e) => { e.stopPropagation(); if (cancelPlay) cancelPlay(); });
    if (overlay) overlay.addEventListener('click', () => { if (cancelPlay) cancelPlay(); });
    document.addEventListener('keydown', (e) => { if (cancelPlay && e.key === 'Escape') { e.preventDefault(); cancelPlay(); } });

    // Web Mercator world units (0..1) and back, and the van Wijk & Nuij zoom-and-pan curve (the one
    // d3.interpolateZoom uses): p = [x, y, w], w = view width in world units, u in 0..1.
    const mx = (lon) => (lon + 180) / 360;
    const my = (lat) => (1 - Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) / Math.PI) / 2;
    const lonOf = (x) => x * 360 - 180;
    const latOf = (y) => (360 / Math.PI) * Math.atan(Math.exp(Math.PI * (1 - 2 * y))) - 90;
    function zoomPath(p0, p1, rho = 1.2) {
      const [x0, y0, w0] = p0, [x1, y1, w1] = p1;
      const dx = x1 - x0, dy = y1 - y0, d2 = dx * dx + dy * dy, r2 = rho * rho;
      if (d2 < 1e-18) { const S = Math.log(w1 / w0); return (u) => [x0 + u * dx, y0 + u * dy, w0 * Math.exp(u * S)]; }
      const d1 = Math.sqrt(d2);
      const b0 = (w1 * w1 - w0 * w0 + r2 * r2 * d2) / (2 * w0 * r2 * d1);
      const b1 = (w1 * w1 - w0 * w0 - r2 * r2 * d2) / (2 * w1 * r2 * d1);
      const r0 = Math.log(Math.sqrt(b0 * b0 + 1) - b0), r1 = Math.log(Math.sqrt(b1 * b1 + 1) - b1);
      const S = (r1 - r0) / rho, c0 = Math.cosh(r0);
      return (u) => {
        const s = rho * u * S + r0;
        const k = (w0 / (r2 * d1)) * (c0 * Math.tanh(s) - Math.sinh(r0));
        return [x0 + k * dx, y0 + k * dy, (w0 * c0) / Math.cosh(s)];
      };
    }

    // The base map's own layers fade in from zoom 6 to 8.5 over the drawn globe, so the dive ends on the same map.
    const OPACITY = { fill: ['fill-opacity'], line: ['line-opacity'], symbol: ['text-opacity', 'icon-opacity'],
      'fill-extrusion': ['fill-extrusion-opacity'], circle: ['circle-opacity'], raster: ['raster-opacity'], background: ['background-opacity'] };
    function fadeIn(l) {
      if (l.id === 'label_city_capital') {  // "Seoul" is our own marker during the dive; the map's label returns after it
        const op = ['interpolate', ['linear'], ['zoom'], 10.5, 0, 11.5, 1];
        return { ...l, paint: { ...l.paint, 'text-opacity': op, 'icon-opacity': op } };
      }
      if ((l.minzoom || 0) >= 8.5) return l;
      const paint = { ...l.paint };
      for (const p of OPACITY[l.type] || []) {
        const v = typeof paint[p] === 'number' ? paint[p] : 1;
        paint[p] = ['interpolate', ['linear'], ['zoom'], 6, 0, 8.5, v];
      }
      return { ...l, minzoom: Math.max(l.minzoom || 0, 6), paint };
    }
    // the coast's tracing stroke: each line drawn up to p (0..1) of its length, with a white pen tip
    function trace(p, c) {
      const none = 'rgba(0,0,0,0)', g = (...stops) => ['interpolate', ['linear'], ['line-progress'], ...stops];
      if (p <= 0) return g(0, none, 1, none);
      if (p >= 1) return g(0, c, 1, c);
      const a = Math.max(p - 0.05, 1e-4), b = Math.max(p, a + 1e-4), e = Math.min(b + 0.002, 1);
      return g(0, c, a, c, b, '#fff', e, none, ...(e < 1 ? [1, none] : []));
    }
    function makeGlobe(topo, start, base) {
      if (!window.maplibregl || !window.topojson || !topo) return null;
      let land;
      try { land = window.topojson.feature(topo[0], topo[0].objects.land); } catch { return null; }
      const korea = topo[1];  // Natural Earth 1:10m: the coast and islands still hold at the zoom the base map takes over
      // every coast and island as its own line, so the tracing stroke (line-progress) runs round each at once
      const coasts = { type: 'FeatureCollection', features: korea.geometry.coordinates.flat().map((ring) => ({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: ring } })) };
      const lines = [];
      for (let lon = -180; lon < 180; lon += 15) lines.push(Array.from({ length: 81 }, (_, i) => [lon, -80 + i * 2]));
      for (let lat = -75; lat <= 75; lat += 15) lines.push(Array.from({ length: 181 }, (_, i) => [-180 + i * 2, lat]));
      const op = (z) => ['interpolate', ['linear'], ['zoom'], ...z];
      try {
        const globe = new window.maplibregl.Map({
          container: globeHost, interactive: false, attributionControl: false, fadeDuration: 0,
          center: [start.lon, start.lat], zoom: start.zoom,
          style: {
            version: 8,
            ...(base ? { glyphs: base.glyphs, sprite: base.sprite } : {}),
            projection: { type: 'globe' },
            sky: { 'atmosphere-blend': op([0, 1, 4, 1, 6.5, 0]) },
            sources: {
              land: { type: 'geojson', data: land },
              korea: { type: 'geojson', data: korea },
              coasts: { type: 'geojson', data: coasts, lineMetrics: true },
              grat: { type: 'geojson', data: { type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates: lines } } },
              // the same vector tiles as the Leaflet map, at the same scale (MapLibre zoom 15 = Leaflet zoom 16)
              // the ladder of zooms (warmTiles); nothing below z6, where the base map has not started to fade in
              ...(base ? { openmaptiles: { type: 'vector', tiles: ['dive://{z}/{x}/{y}'], minzoom: 6, maxzoom: 14 } } : {}),
            },
            layers: [
              { id: 'ocean', type: 'background', paint: { 'background-color': cssVar('--globe-ocean') } },
              // the rest of the land dims as South Korea lights up
              { id: 'land', type: 'fill', source: 'land', paint: { 'fill-color': cssVar('--globe-land'), 'fill-opacity': op([2.6, 1, 3.6, 0.6]) } },
              { id: 'grat', type: 'line', source: 'grat', maxzoom: 5, paint: { 'line-color': cssVar('--globe-grat'), 'line-width': 0.8 } },
              ...(base ? base.layers.filter((l) => l.source === 'openmaptiles' || l.type === 'background').map(fadeIn) : []),
              // South Korea: the coast is traced (render() moves the line-gradient), then it glows and fills
              { id: 'korea-glow', type: 'line', source: 'coasts', maxzoom: 8, layout: { 'line-join': 'round' },
                paint: { 'line-color': cssVar('--accent'), 'line-width': op([2.5, 6, 6, 18]), 'line-blur': op([2.5, 5, 6, 14]), 'line-opacity': op([3.2, 0, 3.8, 0.75, 6, 0.5, 7.5, 0]) } },
              { id: 'korea', type: 'fill', source: 'korea', maxzoom: 8, paint: { 'fill-color': cssVar('--accent'), 'fill-opacity': op([3.3, 0, 3.9, 0.9, 5, 0.75, 6.5, 0.3, 7.5, 0]) } },
              { id: 'korea-edge', type: 'line', source: 'coasts', maxzoom: 8, layout: { 'line-join': 'round', 'line-cap': 'round' },
                paint: { 'line-gradient': trace(0, cssVar('--accent')), 'line-width': op([2, 1.6, 6, 2.6]), 'line-opacity': op([6, 1, 7.5, 0]) } },
            ],
          },
        });
        globe.on('error', () => {});  // off-ladder tiles are refused on purpose; without a listener each one is logged
        return globe;
      } catch { return null; }  // no WebGL
    }

    const getJson = (url) => fetch(url).then((r) => { if (!r.ok) throw new Error(url); return r.json(); });
    const within = (p, ms) => Promise.race([p, new Promise((resolve) => setTimeout(() => resolve(null), ms))]);

    async function loadTowing() {
      const d = await getJson(`/api/towing?lat=${CENTRE[0]}&lon=${CENTRE[1]}&r=1000`);
      const pts = d.points || [];
      if (!pts.length) return null;
      const by = (m) => pts.filter((p) => p.band_m <= m).reduce((n, p) => n + p.count, 0);
      const first = pts.reduce((a, p) => (p.first < a ? p.first : a), pts[0].first);
      const last = pts.reduce((a, p) => (p.last > a ? p.last : a), pts[0].last);
      return { n300: by(300), n500: by(500), n1000: by(1000), span: `${monthLabel(first)} – ${monthLabel(last)}` };
    }

    function buildText(st) {
      const d = st.d, tow = st.tow;
      const H = [
        [0.2, T.about + 0.1, 'Barrier Lens', esc(_('Does the chair fit past it?')), esc(_('A sidewalk check for wheelchair users, built on Meta models'))],
        [T.about, T.dive + 0.1, _('What it does'), esc(_('Ten seconds of video, a measured answer')), '',
          `<ul class="iv-feats">${FEATS.map(([icon, text]) => `<li><svg viewBox="0 0 24 24" aria-hidden="true">${icon}</svg>${esc(text)}</li>`).join('')}</ul>`],
        tow
          ? [T.rings + 0.05, T.cam - 0.05, _('Gwanghwamun · Seoul Open Data'), _('{count} e-scooter towing reports', { count: `<b data-count="${tow.n1000}">0</b>` }),
            `${esc(_('within 1 km of the gate: {n500} within 500 m, {n300} within 300 m', { n500: num(tow.n500), n300: num(tow.n300) }))}<br>${esc(tow.span)}`]
          : [T.rings + 0.05, T.cam - 0.05, _('Gwanghwamun Gate, Seoul, South Korea'), esc(_('Towing reports and station exits around the gate')), ''],
        [T.cam + 0.1, T.spark[0], _('Step 1 · film ten seconds'), esc(_('Can a wheelchair get past this?')), esc(_('Faces are blurred on our server before any model sees the clip.'))],
        [T.spark[0] + 0.05, T.verdict, _('Step 2 · Muse Spark 1.3 and SAM 3.1'), esc(_('Find the barriers, then measure the gap')),
          esc(_('Models find and outline; geometry measures. A model can only make the answer more cautious.'))],
        [T.verdict + 0.05, T.handoff, _('Step 3 · the answer, with its error'), esc(_('Passes: about {n} cm', { n: num(d.gap_cm) })),
          esc(_('95% band {lo}–{hi} cm · a {chair} cm chair needs {need} cm', { lo: num(d.band_cm[0]), hi: num(d.band_cm[1]), chair: num(d.chair_cm), need: num(d.needed_cm) }))],
      ];
      heads.innerHTML = H.map(([, , k, title, sub, more = '']) => `<div class="iv-head"><p class="iv-kicker">${esc(k)}</p><h2 class="iv-title">${title}</h2>${sub ? `<p class="iv-sub">${sub}</p>` : ''}${more}</div>`).join('');
      st.heads = H.map(([a, b], i) => ({ a, b, el: heads.children[i], count: heads.children[i].querySelector('[data-count]') }));
      st.feats = [...heads.querySelectorAll('.iv-feats li')];
      const tags = [[T.blur + 0.15, _('{n} faces blurred · YuNet', { n: `<b>${num(d.blur.faces)}</b>` }), 'blur'],
        [T.spark[0] + 0.35, _('Muse Spark: path {state}', { state: `<b>${esc(PATH_STATE[d.path] || d.path)}</b>` }), 'spark'],
        ...d.barriers.map((b, i) => [T.spark[0] + 0.65 + 0.3 * i, esc(tr(b)), 'spark'])];
      $('#iv-tags').innerHTML = tags.map(([, h, k]) => `<li data-k="${k}">${h}</li>`).join('');
      st.tags = tags.map(([at], i) => ({ at, el: $('#iv-tags').children[i] }));
      $('#iv-hud-cam').textContent = _('camera {h} m · tilt {p}° down', { h: nf(d.camera.height_m, { minimumFractionDigits: 2, maximumFractionDigits: 2 }), p: Math.round(d.camera.pitch_deg) });
      $('#iv-verdict-cm').textContent = `${num(d.gap_cm)} cm`;
      st.rail = [...$('#iv-rail').children];
      st.crumbs = [...crumbs.querySelectorAll('li')];
      st.say = tr(d.explanation);
      // perimeter of each outline, for the tracing stroke
      st.outlines = d.outlines.map((pts) => {
        let len = 0;
        for (let i = 0; i < pts.length; i++) { const a = pts[i], b = pts[(i + 1) % pts.length]; len += Math.hypot(b[0] - a[0], b[1] - a[1]); }
        return { pts, len };
      });
      const [a, b] = d.gap_px;
      zoomBox.style.transformOrigin = `${((a[0] + b[0]) / 2 / d.frame_size[0]) * 100}% ${((a[1] + b[1]) / 2 / d.frame_size[1]) * 100}%`;
    }

    function measure(st) {
      const r = overlay.getBoundingClientRect();
      st.W = r.width; st.H = r.height;
      st.dpr = Math.min(window.devicePixelRatio || 1, 2);
      fx.width = Math.round(st.W * st.dpr); fx.height = Math.round(st.H * st.dpr);
      const g = map.latLngToContainerPoint(L.latLng(CENTRE));
      st.gate = { x: g.x, y: g.y };
      const ringPx = (m) => g.distanceTo(map.latLngToContainerPoint(L.latLng(CENTRE[0] + m / 111320, CENTRE[1])));
      st.rings = st.tow
        ? [[300, '300 m', st.tow.n300], [500, '500 m', st.tow.n500], [1000, '1 km', st.tow.n1000]].map(([m, label, n]) => ({ px: ringPx(m), label, n }))
        : [[500, '500 m', null]].map(([m, label, n]) => ({ px: ringPx(m), label, n }));
      const sb = skipBtn.getBoundingClientRect();
      st.skip = { left: sb.left - r.left, bottom: sb.bottom - r.top };
      const wide = isWide();
      const col = wide ? 448 : 0;
      const w = wide ? Math.min(720, st.W - col - 72) : st.W - 32;
      cam.style.width = `${w}px`;
      cam.style.left = `${wide ? col + (st.W - col - w) / 2 : 16}px`;
      const h = cam.offsetHeight;
      cam.style.top = `${wide ? Math.max(24, (st.H - h) / 2) : Math.max(64, st.H - h - 12)}px`;  // phones: at the bottom, titles above
      st.k = screen.clientWidth / st.d.frame_size[0];
      camFx.width = Math.round(screen.clientWidth * st.dpr); camFx.height = Math.round(screen.clientHeight * st.dpr);
    }

    function pill(ctx, text, x, y, font, align = 'center') {
      ctx.font = font;
      const w = ctx.measureText(text).width + 16, h = 24;
      const left = align === 'center' ? x - w / 2 : x;
      ctx.fillStyle = 'rgba(12,14,19,.78)';
      ctx.beginPath(); ctx.roundRect(left, y - h / 2, w, h, 12); ctx.fill();
      ctx.fillStyle = '#fff'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillText(text, left + 8, y + 1);
    }
    function dot(ctx, p, label, a, t, accent, ink) {
      if (a <= 0) return;
      const pulse = (t / 1.2) % 1;
      ctx.save();
      ctx.globalAlpha = a * (1 - pulse) * 0.8;
      ctx.beginPath(); ctx.arc(p.x, p.y, 7 + 16 * pulse, 0, 2 * Math.PI);
      ctx.strokeStyle = accent; ctx.lineWidth = 2; ctx.stroke();
      ctx.globalAlpha = a;
      ctx.beginPath(); ctx.arc(p.x, p.y, 7, 0, 2 * Math.PI);
      ctx.fillStyle = accent; ctx.fill(); ctx.strokeStyle = ink; ctx.lineWidth = 2; ctx.stroke();
      if (label) pill(ctx, label, p.x + 14, p.y, '650 13px system-ui, sans-serif', 'left');
      ctx.restore();
    }

    // the map layer: Seoul and the gate during the dive, then the rings on the Leaflet map, then the dimming
    function drawMap(st, t, view) {
      const ctx = fx.getContext('2d');
      ctx.setTransform(st.dpr, 0, 0, st.dpr, 0, 0);
      ctx.clearRect(0, 0, st.W, st.H);
      const out = 1 - seg(t, T.handoff, T.handoff + 0.45);
      if (view && st.globe) {
        const p = st.globe.project(SEOUL);
        const ping = seg(view.zoom, 3.5, 3.9) * (1 - seg(view.zoom, 4.8, 5.4));  // South Korea lit: a sonar ping from Seoul
        if (ping > 0) {
          const R = 0.16 * Math.min(st.W, st.H);
          ctx.save(); ctx.strokeStyle = st.accent; ctx.lineWidth = 2;
          for (let k = 0; k < 3; k++) {
            const ph = (t / 1.2 + k / 3) % 1;
            ctx.globalAlpha = ping * (1 - ph) * 0.8;
            ctx.beginPath(); ctx.arc(p.x, p.y, 6 + R * outCubic(ph), 0, 2 * Math.PI); ctx.stroke();
          }
          ctx.restore();
        }
        const ka = seg(view.zoom, 3.4, 3.8) * (1 - seg(view.zoom, 4.5, 4.9));  // our label leaves as the map's own fades in
        if (ka > 0) {
          const k = st.globe.project(KOREA_TAG);
          ctx.save(); ctx.globalAlpha = ka; pill(ctx, _('South Korea'), k.x + 10, k.y, '650 13px system-ui, sans-serif', 'left'); ctx.restore();
        }
        dot(ctx, p, _('Seoul'), seg(view.zoom, 4, 4.8) * (1 - seg(view.zoom, 9.5, 10.5)), t, st.accent, st.ink);
      }
      if (t >= T.rings) {
        const rp = seg(t, T.rings, T.rings + 1.3), big = st.rings[st.rings.length - 1].px;
        const placed = [];  // ring labels pushed to the same edge stack instead of covering each other
        if (rp < 1) {
          ctx.globalAlpha = 0.6 * (1 - rp);
          ctx.beginPath(); ctx.arc(st.gate.x, st.gate.y, big * 1.15 * outCubic(rp), 0, 2 * Math.PI);
          ctx.strokeStyle = st.accent; ctx.lineWidth = 3; ctx.stroke();
          ctx.globalAlpha = 1;
        }
        st.rings.forEach((rg, i) => {
          const s = T.rings + i * T.ringGap, p = outCubic(seg(t, s, s + T.ringDur));
          if (p <= 0) return;
          ctx.globalAlpha = out;
          ctx.beginPath(); ctx.arc(st.gate.x, st.gate.y, rg.px * p, 0, 2 * Math.PI);
          ctx.fillStyle = st.towColour; ctx.globalAlpha = 0.08 * p * out; ctx.fill();
          ctx.globalAlpha = out; ctx.setLineDash([10, 7]); ctx.strokeStyle = st.towColour; ctx.lineWidth = 3; ctx.stroke(); ctx.setLineDash([]);
          const la = seg(t, s + 0.45, s + 0.8) * out * (1 - seg(t, T.cam - 0.2, T.cam + 0.3));  // labels leave when the viewfinder comes in
          if (la > 0) {
            const ang = -Math.PI / 3;
            const x = Math.min(st.W - 70, Math.max(70, st.gate.x + rg.px * Math.cos(ang)));
            let y = Math.min(st.H - 20, Math.max(20, st.gate.y + rg.px * Math.sin(ang)));
            if (x + 70 > st.skip.left && y - 14 < st.skip.bottom) y = st.skip.bottom + 20;  // keep clear of the skip button
            while (placed.some((q) => Math.abs(q.x - x) < 120 && Math.abs(q.y - y) < 26)) y += 28;
            placed.push({ x, y });
            ctx.globalAlpha = la;
            pill(ctx, rg.n == null ? rg.label : `${rg.label} · ${num(rg.n * outCubic(seg(t, s + 0.3, s + 1.0)))}`, x, y, '700 13px system-ui, sans-serif');
          }
          ctx.globalAlpha = 1;
        });
      }
      const gp = view && st.globe ? st.globe.project(GATE) : st.gate;
      const ga = view ? seg(view.zoom, 10, 11) : out;
      dot(ctx, gp, t < T.cam + 0.45 ? _('Gwanghwamun Gate') : '', ga, t, st.accent, st.ink);
      const dim = 0.55 * seg(t, T.cam - 0.1, T.cam + 0.45) * out;
      if (dim > 0) { ctx.fillStyle = `rgba(6,8,12,${dim})`; ctx.fillRect(0, 0, st.W, st.H); }
    }

    // the viewfinder layer: Muse Spark's scan, SAM 3.1's outlines, the ruler (frame pixels)
    function drawCam(st, t) {
      const ctx = camFx.getContext('2d');
      const s = st.k * st.dpr, d = st.d;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, camFx.width, camFx.height);
      ctx.setTransform(s, 0, 0, s, 0, 0);
      const [FW, FH] = d.frame_size;
      const u = Math.max(1, 0.5 / st.k);  // keeps the ruler's labels readable on a phone-sized viewfinder
      if (t > T.spark[0] && t < T.spark[1] + 0.2) {
        const y = FH * inOut(seg(t, T.spark[0], T.spark[1]));
        const a = 1 - seg(t, T.spark[1], T.spark[1] + 0.2);
        const g = ctx.createLinearGradient(0, y - 120, 0, y);
        g.addColorStop(0, 'rgba(255,210,63,0)'); g.addColorStop(1, `rgba(255,210,63,${0.32 * a})`);
        ctx.fillStyle = g; ctx.fillRect(0, y - 120, FW, 120);
        ctx.fillStyle = `rgba(255,226,120,${0.95 * a})`; ctx.fillRect(0, y - 2, FW, 4);
      }
      if (t >= T.sam[0]) {
        const dimTo = 1 - 0.45 * seg(t, T.measure[0], T.measure[0] + 0.4);
        st.outlines.forEach((o, i) => {
          const s0 = T.sam[0] + i * 0.09, p = outCubic(seg(t, s0, s0 + 0.55));
          if (p <= 0) return;
          ctx.beginPath();
          o.pts.forEach(([x, y], j) => (j ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
          ctx.closePath();
          ctx.fillStyle = `rgba(255,210,63,${0.3 * seg(t, s0 + 0.35, s0 + 0.75) * dimTo})`; ctx.fill();
          ctx.setLineDash([o.len * p, o.len]);
          ctx.strokeStyle = `rgba(255,210,63,${dimTo})`; ctx.lineWidth = 4; ctx.lineJoin = 'round'; ctx.stroke();
          ctx.setLineDash([]);
        });
      }
      if (t >= T.measure[0] + 0.1) {
        const [e0, e1] = d.gap_px;
        const m = [(e0[0] + e1[0]) / 2, (e0[1] + e1[1]) / 2];
        const p = outCubic(seg(t, T.measure[0] + 0.1, T.measure[0] + 0.55));
        const a = [m[0] + (e0[0] - m[0]) * p, m[1] + (e0[1] - m[1]) * p];
        const b = [m[0] + (e1[0] - m[0]) * p, m[1] + (e1[1] - m[1]) * p];
        const len = Math.hypot(e1[0] - e0[0], e1[1] - e0[1]);
        const nx = -(e1[1] - e0[1]) / len, ny = (e1[0] - e0[0]) / len;  // unit normal (points down-ish)
        const line = (p0, p1, w, c) => { ctx.beginPath(); ctx.moveTo(p0[0], p0[1]); ctx.lineTo(p1[0], p1[1]); ctx.lineWidth = w; ctx.strokeStyle = c; ctx.lineCap = 'round'; ctx.stroke(); };
        line(a, b, 11 * u, 'rgba(0,0,0,.55)');
        line(a, b, 6 * u, '#ffd23f');
        const ca = seg(t, T.measure[0] + 0.5, T.measure[0] + 0.65);
        if (ca > 0) {
          ctx.globalAlpha = ca;
          for (const e of [e0, e1]) line([e[0] - nx * 22 * u, e[1] - ny * 22 * u], [e[0] + nx * 22 * u, e[1] + ny * 22 * u], 6 * u, '#ffd23f');
          ctx.globalAlpha = 1;
        }
        const text = (str, x, y, size, fill) => {
          ctx.font = `800 ${size}px system-ui, sans-serif`; ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
          ctx.lineWidth = size * 0.22; ctx.strokeStyle = 'rgba(0,0,0,.75)'; ctx.lineJoin = 'round';
          ctx.strokeText(str, x, y); ctx.fillStyle = fill; ctx.fillText(str, x, y);
        };
        text(`${num(d.gap_cm * outCubic(seg(t, T.measure[0] + 0.2, T.measure[0] + 0.9)))} cm`, m[0] - nx * 30 * u, m[1] - ny * 30 * u, 56 * u, '#fff');
        const ba = seg(t, T.measure[0] + 0.7, T.measure[0] + 0.95);
        if (ba > 0) { ctx.globalAlpha = ba; text(_('95% band {lo}–{hi} cm', { lo: num(d.band_cm[0]), hi: num(d.band_cm[1]) }), m[0] + nx * 52 * u, m[1] + ny * 52 * u, 26 * u, '#fff'); ctx.globalAlpha = 1; }
        const na = seg(t, T.measure[0] + 0.85, T.measure[0] + 1.1);
        if (na > 0) {
          const half = (len * d.needed_cm) / d.gap_cm / 2, ux = (e1[0] - e0[0]) / len, uy = (e1[1] - e0[1]) / len;
          const c = [m[0] + nx * 84 * u, m[1] + ny * 84 * u];
          ctx.globalAlpha = na;
          line([c[0] - ux * half, c[1] - uy * half], [c[0] + ux * half, c[1] + uy * half], 11 * u, 'rgba(0,0,0,.55)');
          line([c[0] - ux * half, c[1] - uy * half], [c[0] + ux * half, c[1] + uy * half], 6 * u, '#3ddc84');
          text(_('needs {n} cm', { n: num(d.needed_cm) }), c[0] + nx * 40 * u, c[1] + ny * 40 * u, 26 * u, '#3ddc84');
          ctx.globalAlpha = 1;
        }
      }
    }

    // Fritsch-Carlson monotone cubic through [[x, y], ...], flat at both ends
    function monotone(keys) {
      const n = keys.length, h = [], dl = [], m = new Array(n).fill(0);
      for (let i = 0; i < n - 1; i++) { h[i] = keys[i + 1][0] - keys[i][0]; dl[i] = (keys[i + 1][1] - keys[i][1]) / h[i]; }
      for (let i = 1; i < n - 1; i++) m[i] = dl[i - 1] * dl[i] <= 0 ? 0 : (3 * (h[i - 1] + h[i])) / ((2 * h[i] + h[i - 1]) / dl[i - 1] + (h[i] + 2 * h[i - 1]) / dl[i]);
      return (x) => {
        if (x <= keys[0][0]) return keys[0][1];
        if (x >= keys[n - 1][0]) return keys[n - 1][1];
        let i = 0;
        while (x > keys[i + 1][0]) i++;
        const s = (x - keys[i][0]) / h[i], s2 = s * s, s3 = s2 * s;
        return (2 * s3 - 3 * s2 + 1) * keys[i][1] + (s3 - 2 * s2 + s) * h[i] * m[i] + (-2 * s3 + 3 * s2) * keys[i + 1][1] + (s3 - s2) * h[i] * m[i + 1];
      };
    }
    function camera(st, t) {
      const z = st.zoomAt(t);
      // path parameter whose zoom is z (the table is monotone in zoom)
      const tb = st.uz;
      let i = 1;
      while (i < tb.length - 1 && tb[i][1] < z) i++;
      const [u0, z0] = tb[i - 1], [u1, z1] = tb[i];
      const [x, y] = st.path(z1 > z0 ? u0 + ((z - z0) / (z1 - z0)) * (u1 - u0) : u1);
      // the swoop: over South Korea the camera tilts toward the horizon and turns; it is level again before the base
      // map fades in (zoom 6), so a tilted view never asks for far-off tiles, and the turn unwinds on the way down
      const tilt = inOut(seg(t, 1.9, 2.6)) * (1 - inOut(seg(t, 2.95, 3.45)));
      const turn = inOut(seg(t, 1.8, 2.7)) * (1 - inOut(seg(t, 3.1, 4.6)));
      return { lon: lonOf(x), lat: latOf(y), zoom: z, pitch: 55 * tilt, bearing: -32 * turn };
    }

    function render(st, t, live) {
      overlay.dataset.t = t.toFixed(2);  // the clock, for checks and screen captures
      const view = st.globe && t < T.swap[1] ? camera(st, t) : null;
      if (view) {
        st.globe.jumpTo({ center: [view.lon, view.lat], zoom: view.zoom, pitch: view.pitch, bearing: view.bearing });
        const p = seg(view.zoom, 2.5, 3.6);
        if (p !== st.traced) { st.traced = p; st.globe.setPaintProperty('korea-edge', 'line-gradient', trace(p, st.accent)); }
        st.globe.redraw();  // draw now, so the dots on the canvas above stay on the same frame
      }
      const gOut = st.hadGlobe ? 1 - seg(t, T.swap[0], T.swap[1]) : 0;
      globeHost.style.opacity = gOut;
      $('#iv-mapcredit').style.opacity = gOut;
      if (live && st.globe && t >= T.swap[1]) { st.globe.remove(); st.globe = null; }
      document.body.classList.toggle('intro-on', t < T.handoff);

      // titles and crumbs
      st.heads.forEach((h) => {
        const o = fadeIO(t, h.a, h.b, 0.3);
        h.el.style.opacity = o;
        h.el.style.transform = `translateY(${(1 - outCubic(seg(t, h.a, h.a + 0.45))) * 16}px)`;
        if (h.count) h.count.textContent = num(Number(h.count.dataset.count) * outCubic(seg(t, T.rings + 0.1, T.rings + 1.25)));
      });
      st.feats.forEach((li, i) => {
        const s = T.about + 0.3 + 0.35 * i;
        li.style.opacity = seg(t, s, s + 0.3);
        li.hidden = t < s;  // the card grows a line at a time
        li.style.transform = `translateX(${(1 - outCubic(seg(t, s, s + 0.4))) * -12}px)`;
      });
      crumbs.style.opacity = st.hadGlobe ? fadeIO(t, 0.15, T.swap[1] + 0.1) : 0;
      let lastOn = -1;
      st.crumbs.forEach((li, i) => { const o = seg(t, st.crumbT[i], st.crumbT[i] + 0.25); li.style.opacity = o; li.hidden = o === 0; if (o > 0) lastOn = i; });
      st.crumbs.forEach((li, i) => li.classList.toggle('on', i === lastOn));
      if (view) {
        $('#iv-coords').textContent = `${Math.abs(view.lat).toFixed(2)}° ${view.lat >= 0 ? 'N' : 'S'} · ${Math.abs(((view.lon + 540) % 360) - 180).toFixed(2)}° ${((view.lon + 540) % 360) - 180 >= 0 ? 'E' : 'W'}`;
      }

      // station exits pop up on the real map, nearest first
      if (t >= T.exits - 0.05 && !st.pins.length) {
        const m = map.getContainer().getBoundingClientRect();
        st.pins = [...document.querySelectorAll('#map .exit-pin')].map((el) => {
          const r = el.getBoundingClientRect();
          return { el, d: Math.hypot(r.left + r.width / 2 - m.left - st.gate.x, r.top + r.height / 2 - m.top - st.gate.y) };
        }).sort((p, q) => p.d - q.d).map((p) => p.el);
      }
      st.pins.forEach((el, i) => {
        const s = T.exits + i * 0.05, p = outBack(seg(t, s, s + 0.35));
        el.style.opacity = clamp01(p * 3);
        el.style.transform = `scale(${p})`;
      });

      // the viewfinder
      const ci = outCubic(seg(t, T.cam, T.cam + 0.6)), co = seg(t, T.handoff, T.handoff + 0.45);
      cam.style.opacity = seg(t, T.cam, T.cam + 0.35) * (1 - co);
      cam.style.transform = `translateY(${(1 - ci) * 40}px) scale(${(0.94 + 0.06 * ci) * (1 - 0.05 * co)})`;
      if (live) {
        if (t >= T.play && t < T.freeze && !st.played) { st.played = true; try { video.currentTime = 0; } catch { /* not loaded */ } video.play().catch(() => {}); }
        if (t >= T.freeze && !video.paused) video.pause();
      } else {
        const ct = Math.min(Math.max(t - T.play, 0), 2.9);
        if (video.readyState >= 1 && Math.abs(video.currentTime - ct) > 0.02) video.currentTime = ct;
      }
      still.style.opacity = seg(t, T.freeze, T.freeze + 0.08);
      $('#iv-flash').style.opacity = t >= T.freeze ? 0.8 * (1 - seg(t, T.freeze, T.freeze + 0.35)) : 0;
      zoomBox.style.transform = `scale(${1 + 0.06 * inOut(seg(t, T.freeze, T.end))})`;
      st.tags.forEach((g) => {
        g.el.style.opacity = fadeIO(t, g.at, T.measure[0], 0.25);
        g.el.style.transform = `translateX(${(1 - outCubic(seg(t, g.at, g.at + 0.35))) * -14}px)`;
      });
      $('#iv-hud-cam').parentElement.style.opacity = 1 - seg(t, T.freeze, T.freeze + 0.2);
      const v = $('#iv-verdict');
      v.style.opacity = seg(t, T.verdict, T.verdict + 0.15);
      v.style.transform = `scale(${outBack(seg(t, T.verdict, T.verdict + 0.4))})`;
      const n = Math.round(st.say.length * seg(t, T.explain[0], T.explain[1]));
      $('#iv-say').innerHTML = n ? `“${esc(st.say.slice(0, n))}${n === st.say.length ? `” <cite>Muse Spark 1.3</cite>` : ''}` : '';
      st.rail.forEach((li, i) => { li.dataset.state = t >= RAIL[i + 1] ? 'done' : t >= RAIL[i] ? 'run' : ''; });

      drawMap(st, t, view);
      if (t >= T.cam) drawCam(st, t);
    }

    // The dive passes through about 9.5 MB of OpenFreeMap tiles in central Seoul, more than a phone connection fetches
    // in its five seconds, and tiles that land late pop in at random. So the base map is drawn only at a ladder of
    // zooms, fetched before the dive starts (about 1.9 MB; z14 is the landing view the page's own map loads anyway):
    // MapLibre asks for dive://z/x/y, a ladder tile comes from the network (the HTTP cache by then; these tiles never
    // change under one URL), any other zoom is refused and MapLibre draws the ladder tile below it, overzoomed.
    const LADDER = [6, 9, 12, 14];
    let diveProtocol = false;
    function warmTiles(st, url) {
      if (!diveProtocol && window.maplibregl && maplibregl.addProtocol) {
        diveProtocol = true;
        maplibregl.addProtocol('dive', async ({ url: u }, abort) => {
          const [z, x, y] = u.slice('dive://'.length).split('/');
          if (!LADDER.includes(Number(z))) throw new Error('not on the ladder');  // MapLibre falls back to the parent
          const r = await fetch((await tileUrl).replace('{z}', z).replace('{x}', x).replace('{y}', y), { signal: abort.signal });
          if (!r.ok) throw new Error(`tile ${z}/${x}/${y}: HTTP ${r.status}`);
          return { data: await r.arrayBuffer() };
        });
      }
      const keys = new Set();
      for (let i = 0; i <= 240; i++) {
        const v = camera(st, (i / 240) * T.dive);
        const z = Math.min(14, Math.floor(v.zoom)), n = 2 ** z, k = 512 * 2 ** (v.zoom - z);  // k: a tile's size on screen
        if (!LADDER.includes(z)) continue;
        const b = (v.bearing * Math.PI) / 180, c = Math.abs(Math.cos(b)), s2 = Math.abs(Math.sin(b));
        const hx = (st.W * c + st.H * s2) / 2 / k, hy = (st.W * s2 + st.H * c) / 2 / k;  // the turned view's bounding box
        const cx = mx(v.lon) * n, cy = my(v.lat) * n;
        for (let x = Math.floor(cx - hx); x <= Math.floor(cx + hx); x++) {
          for (let y = Math.floor(cy - hy); y <= Math.floor(cy + hy); y++) keys.add(`${z}/${x}/${y}`);
        }
      }
      const pool = (keyList) => {  // six at a time, in order
        const urls = keyList.map((key) => { const [z, x, y] = key.split('/'); return url.replace('{z}', z).replace('{x}', x).replace('{y}', y); });
        let i = 0;
        const next = () => (i < urls.length ? fetch(urls[i++]).then((r) => r.arrayBuffer()).catch(() => null).then(next) : null);
        return Promise.all(Array.from({ length: 6 }, next));
      };
      const low = [...keys].filter((k) => Number(k.split('/')[0]) <= 9);
      const first = pool(low);  // z6 and z9: the dive starts when these are in
      return { first, all: first.then(() => pool([...keys].filter((k) => !low.includes(k)))) };
    }

    function finish(st) {
      if (st.done) return;
      st.done = true;
      cancelPlay = null;
      cancelAnimationFrame(st.raf);
      window.removeEventListener('resize', st.onResize);
      if (st.globe) { try { st.globe.remove(); } catch { /* gone */ } st.globe = null; }
      video.pause(); video.removeAttribute('src'); video.load();
      st.pins.forEach((el) => { el.style.opacity = ''; el.style.transform = ''; });
      for (const c of [fx, camFx]) c.getContext('2d').clearRect(0, 0, c.width, c.height);
      document.body.classList.remove('intro-on');
      overlay.classList.remove('static');
      overlay.hidden = true;
      if (skipBtn && document.activeElement === skipBtn) skipBtn.blur();
      st.resolve();
    }

    // Reduced motion: no globe and no motion; the location line alone for 3.5 s.
    function playStatic() {
      return new Promise((resolve) => {
        const st = { done: false, pins: [], resolve, onResize: null };
        overlay.hidden = false;
        overlay.classList.add('static');
        heads.innerHTML = '';
        for (const el of [globeHost, cam, $('#iv-mapcredit')]) el.style.opacity = 0;
        crumbs.style.opacity = '';
        crumbs.querySelectorAll('li').forEach((li) => { li.style.opacity = ''; li.hidden = false; li.classList.remove('on'); });
        crumbs.querySelector('li:last-child').classList.add('on');
        $('#iv-coords').textContent = '37.58° N · 126.98° E';
        cancelPlay = () => finish(st);
        setTimeout(() => finish(st), 3500);
      });
    }

    // the camera path for this window: van Wijk & Nuij from the globe to the map's view, the zoom keyed to time
    function plan(st) {
      const r = overlay.getBoundingClientRect();
      st.W = r.width; st.H = r.height;
      const c = map.getCenter();
      const end = [mx(c.lng), my(c.lat), st.W / (512 * 2 ** (map.getZoom() - 1))];
      const z0 = Math.log2((0.36 * Math.min(st.W, st.H) * 2 * Math.PI) / 512);
      st.path = zoomPath([mx(START[0]), my(START[1]), st.W / (512 * 2 ** z0)], end);
      st.uz = [];
      for (let i = 0, top = -Infinity; i <= 400; i++) {
        top = Math.max(top, Math.log2(st.W / (512 * st.path(i / 400)[2])));
        st.uz.push([i / 400, top]);
      }
      const zEnd = map.getZoom() - 1;
      st.zoomAt = monotone(ZKEYS.map(([t, z], i) => [t, i === 0 ? z0 : i === ZKEYS.length - 1 ? zEnd : z]));
    }

    function playAnimated() {
      return new Promise((resolve) => {
        const st = { done: false, pins: [], resolve, played: false, globe: null, raf: 0 };
        cancelPlay = () => finish(st);
        centreOn(L.latLng(CENTRE), 16);  // the view the intro lands on, before the panel slides away
        overlay.classList.remove('static');
        overlay.hidden = false;
        heads.innerHTML = '';
        globeHost.style.opacity = 1;
        st.accent = cssVar('--accent'); st.ink = cssVar('--accent-ink'); st.towColour = cssVar('--tow');
        plan(st);
        const warm = tileUrl.then((url) => warmTiles(st, url)).catch(() => null);  // first: the dive needs them first
        video.muted = true;
        within(warm.then((w) => w && w.first), 2500).then(() => {  // the clip plays seconds later; it waits its turn
          if (st.done) return;
          video.src = '/static/demo/intro-corridor.mp4';
          video.preload = 'auto';
        });
        still.src = '/static/demo/intro-corridor-77.jpg';
        const topo = Promise.all([getJson('/static/vendor/land-110m.json'), getJson('/static/vendor/korea-10m.json')]).catch(() => null);
        Promise.all([getJson('/static/demo/intro-corridor.json'), within(loadTowing().catch(() => null), 2500), within(topo, 2500),
          within(baseStyle.catch(() => null), 2500)])
          .then(([d, tow, topoJson, base]) => {
            if (st.done) return;
            st.d = d; st.tow = tow;
            buildText(st);
            measure(st);
            // when each crumb lights up, read off the camera path
            const when = [() => true, (v) => v.lon > 70, (v) => v.zoom > 3.6, (v) => v.zoom > 7.2, (v) => v.zoom > 11.2];
            st.crumbT = when.map(() => T.dive);
            for (let i = 0, k = 0; i <= 320 && k < when.length; i++) {
              const t = (i / 320) * T.dive;
              while (k < when.length && when[k](camera(st, t))) st.crumbT[k++] = t;
            }
            st.crumbT = st.crumbT.map((t, i) => Math.max(t, i * 0.3));
            const start = camera(st, 0);
            st.globe = topoJson ? makeGlobe(topoJson, start, base) : null;
            const ready = st.globe ? within(new Promise((r) => st.globe.once('load', r)), 2500) : Promise.resolve();
            return Promise.all([ready, st.globe ? within(warm.then((w) => w && w.first), 2500) : null]).then(([ok]) => {
              if (st.done) return;
              if (st.globe && ok === null) { st.globe.remove(); st.globe = null; }  // style never loaded
              st.hadGlobe = !!st.globe;
              st.onResize = () => measure(st);
              window.addEventListener('resize', st.onResize);
              if (skipBtn) { try { skipBtn.focus({ preventScroll: true }); } catch { skipBtn.focus(); } }
              const from = AT != null ? AT : st.globe ? 0 : T.swap[0];
              const live = AT == null || params.has('go');
              const t0 = performance.now() - from * 1000;
              const frame = (now) => {
                if (st.done) return;
                const t = live ? (now - t0) / 1000 : from;
                render(st, t, live);
                if (live && t >= T.end) { finish(st); return; }
                st.raf = requestAnimationFrame(frame);
              };
              st.raf = requestAnimationFrame(frame);
            });
          })
          .catch(() => finish(st));  // the intro's own data is missing: leave the page as it is
      });
    }

    playIntroNow = (opts = {}) => {
      if (!overlay || !skipBtn) return Promise.resolve();
      if (location.hash === '#nointro') return Promise.resolve();
      if (!opts.force && AT == null && (store.get(INTRO_PREF) === 'off' || skipOnce)) return Promise.resolve();
      if (cancelPlay) cancelPlay();  // a replay never overlaps a running intro
      return reduceMq.matches ? playStatic() : playAnimated();
    };
  }

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
    if (!towingLoaded) {
      return _('Gwanghwamun & Gyeongbokgung. Past e-scooter towing reports within the ring (Seoul open data) and both stations’ elevators, live from Seoul Metro.');
    }
    const short = st.length > 150 ? `${st.slice(0, 150)}…` : st;
    return `${tow} ${_('Both stations’ elevators are live from Seoul Metro: {status}', { status: short })}`;
  }
  function tourCautiousText() {
    const r = check.result;
    const p = r && r.triage && r.triage.path;
    if (p && p.state && p.evidence && r.verdict && VERDICT_WORD[r.verdict]) {
      return _('Muse Spark read “{evidence}” — the path is {state} — so the answer is {verdict} whatever the geometry measured. A model may only make the answer more cautious.',
        { evidence: tr(p.evidence), state: PATH_STATE[p.state] || p.state, verdict: VERDICT_WORD[r.verdict] });
    }
    return _('The triage model reads the clip first. A model may only make the answer more cautious, never looser.');
  }
  function tourMeasuredText() {
    const r = check.result;
    if (r && r.gap_cm != null && Array.isArray(r.band_cm)) {
      const chair = Math.round((r.assumptions && r.assumptions.chair_width_cm)
        || (r.measurement_detail && r.measurement_detail.wheelchair_cm) || 65);
      return _('With the camera’s height and tilt known, geometry measures the gap from SAM 3.1’s outlines: about {gap} cm, range {lo}–{hi}, so a {chair} cm chair fits.',
        { gap: Math.round(r.gap_cm), lo: Math.round(r.band_cm[0]), hi: Math.round(r.band_cm[1]), chair });
    }
    return _('With the camera’s height and tilt known, geometry measures the gap from SAM 3.1’s outlines, with an error range instead of a single number.');
  }
  const TOUR_STEPS = [
    { id: 'map', title: _('Gwanghwamun & Gyeongbokgung'), dur: 6,
      targets: [['#towing-stat', '#station-line']],
      text: tourMapText,
      enter: async () => { tourShowOverview(); setSheet('half'); tourCentre(); } },
    { id: 'check', title: _('Film ten seconds'), dur: 5,
      targets: [['#clip', '#tilt-row']],
      text: () => _('You film ten seconds at seated height. Faces are blurred on our server before any model sees the clip; the phone’s tilt goes with it.'),
      enter: async () => { tourShowCheck(); } },
    { id: 'pipeline', title: _('The pipeline, replayed'), dur: 8, sample: 'closed',
      targets: [['#stages']],
      text: () => _('Five stages: blur → Muse Spark triage → SAM 3.1 outlines → geometry measures → Muse Spark explains. Below is a replay of the recorded run’s stage times.'),
      enter: (tok) => tourPlaySample(tok, 'closed', true) },
    { id: 'cautious', title: _('The model can only be more cautious'), dur: 9,
      targets: [['#v-saw', '#verdict']],
      text: tourCautiousText },
    { id: 'unknown', title: _('An honest Unknown'), dur: 8, sample: 'scooters',
      targets: [['#v-fixit'], ['#verdict']],
      text: () => _('No gap lies straight ahead, so there is nothing honest to measure: Unknown, with how to film again.'),
      enter: (tok) => tourPlaySample(tok, 'scooters', false) },
    { id: 'measured', title: _('Measured, with its error'), dur: 12, sample: 'known',
      targets: [['#stage']], thenTargets: [['#v-bar'], ['#verdict']], thenAt: 0.5,
      text: tourMeasuredText,
      enter: (tok) => tourPlaySample(tok, 'known', false) },
    { id: 'calib', title: _('Calibrate once'), dur: 7,
      targets: [['#calib']],
      text: () => _('Calibrate once: chair width, armrest height, and three seconds of four A4 sheets on the floor. Then your own checks can become decisive.'),
      enter: async () => { tourShowCheck(); $('#calib').open = true; } },
  ];
  const TOUR_END_TEXT = _('That was the product, replayed from recorded live runs (Muse Spark 1.3 and SAM 3.1 on the Meta Model API). Live checks on your own clip need the judge token.');

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
    ol.setAttribute('aria-label', _('Replay of the recorded run’s stage times'));
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
    tourPauseBtn.textContent = p ? _('▶ Resume') : _('⏸ Pause');
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
    $('#tour-title').textContent = _('That was the product');
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
    $('#stages').setAttribute('aria-label', _('Live check progress'));
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
    $('#stages').setAttribute('aria-label', _('Live check progress'));
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
    Promise.race([boot, new Promise((r) => setTimeout(r, 2000))])
      .then(() => introDone).then(() => startTour(), () => startTour());
  }
  const introReplayBtn = $('#tour-intro');
  if (introReplayBtn) introReplayBtn.addEventListener('click', () => {
    tourCloseFull();
    introDone = playIntroNow({ force: true });
  });
  // Intro on every load, unless turned off: the header button (wide screens) or the switch in the language menu.
  // Turning it back on plays it now.
  const introToggle = $('#intro-toggle');
  const introPref = $('#intro-pref');
  function showIntroPref(on) {
    const label = on ? _('Intro plays on every load. Turn it off') : _('Intro off. Turn it on and play it');
    introToggle.setAttribute('aria-pressed', String(on));
    introToggle.setAttribute('aria-label', label);
    introToggle.title = label;
    introPref.checked = on;
  }
  function setIntroPref(on) {
    store.set(INTRO_PREF, on ? 'on' : 'off');
    showIntroPref(on);
    if (!on) return;
    $('#prefs').hidePopover?.();
    if (tour.active) tourCloseFull();
    introDone = playIntroNow({ force: true });
  }
  showIntroPref(store.get(INTRO_PREF) !== 'off');
  introToggle.addEventListener('click', () => setIntroPref(store.get(INTRO_PREF) === 'off'));
  introPref.addEventListener('change', () => setIntroPref(introPref.checked));

  // Language menu: the page reloads in the chosen language (map names, model replies and all).
  $('#lang-code').textContent = { 'zh-CN': '简', 'zh-TW': '繁' }[lang] || lang.toUpperCase();
  $('#lang-list').innerHTML = window.I18N.LANGS.map(([id, name]) => `<label lang="${id}"><input type="radio" name="lang" value="${id}"${id === lang ? ' checked' : ''}> ${esc(name)}</label>`).join('');
  $('#lang-list').addEventListener('change', (e) => window.I18N.set(e.target.value));

  // ------------------------------------------------------------------ start
  layout();
  centreOn(L.latLng(CENTRE), 16);
  const bootData = Promise.allSettled([loadTowing(500), loadStation()]);
  if (params.get('check') === '1' || params.get('sample')) enterCheck();
  if (params.get('sample')) runSample(false, crosshairLatLng());
  if (params.get('open') === '1') setSheet('half');
  introDone = playIntroNow({ force: location.hash === '#tour' });
  maybeTour(bootData);
})();

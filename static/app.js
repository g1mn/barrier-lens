/* Barrier Lens front end: map of Yangjae Station + "Check a spot" flow. No build step. */
(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const YANGJAE = [37.4840283, 127.0346411];
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
  const map = L.map('map', { zoomControl: false, attributionControl: true }).setView(YANGJAE, 16);
  L.control.zoom({ position: 'topright' }).addTo(map);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  map.attributionControl.addAttribution('Towing, elevators: <a href="https://data.seoul.go.kr/">Seoul Open Data</a> (KOGL Type 1)');

  const towLayer = L.layerGroup().addTo(map);
  const exitLayer = L.layerGroup().addTo(map);
  const pinLayer = L.layerGroup().addTo(map);
  let ring = null;
  const towMarkers = new Map();

  // ------------------------------------------------------------------ layout: bar height, sheet, crosshair
  const sheet = $('#sheet');
  const handle = $('#sheet-handle');
  const crosshair = $('#crosshair');

  function barH() { return $('.bar').offsetHeight; }
  function isWide() { return window.matchMedia('(min-width: 900px)').matches; }
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
      const res = await fetch(`/api/towing?lat=${YANGJAE[0]}&lon=${YANGJAE[1]}&r=${r}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      d = await res.json();
    } catch (e) {
      stat.textContent = `Could not load towing history (${e.message}).`;
      return;
    }
    towLayer.clearLayers();
    towMarkers.clear();
    if (ring) ring.remove();
    ring = L.circle(YANGJAE, { radius: r, color: '#111', weight: 2, dashArray: '6 6', fill: false, interactive: false }).addTo(map);

    const pts = d.points || [];
    // big circles first so small ones stay clickable on top
    [...pts].sort((a, b) => b.count - a.count).forEach((p) => {
      const radius = Math.min(22, 4 + 2.4 * Math.sqrt(p.count));
      const m = L.circleMarker([p.lat, p.lon], {
        radius, color: getCss('--tow-stroke'), weight: 1.5, fillColor: getCss('--tow'), fillOpacity: 0.78,
      }).bindPopup(towPopup(p), { maxWidth: 280 });
      m.addTo(towLayer);
      towMarkers.set(p.addr, m);
    });

    const cov = d.coverage || {};
    if (!pts.length) {
      stat.textContent = cov.records == null
        ? 'Towing history is not cached yet (run scripts/fetch_seoul.py).'
        : `No towing reports in the cached data within ${r} m.`;
    } else {
      const first = pts.reduce((a, p) => (p.first < a ? p.first : a), pts[0].first);
      const last = pts.reduce((a, p) => (p.last > a ? p.last : a), pts[0].last);
      stat.innerHTML = `<strong>${d.records.toLocaleString('en')}</strong> e-scooter towing reports at ${pts.length} addresses within ${r >= 1000 ? `${r / 1000} km` : `${r} m`} · ${monthLabel(first)} – ${monthLabel(last)}`;
    }

    const list = $('#hotspots');
    list.innerHTML = '';
    [...pts].sort((a, b) => b.count - a.count).slice(0, 5).forEach((p) => {
      const li = document.createElement('li');
      li.innerHTML = `<button type="button"><span class="dot" aria-hidden="true"></span>
        <span class="addr" lang="ko">${esc(shortAddr(p.addr))}<span class="meta" lang="en">${p.dist_m} m from the station · last ${monthLabel(p.last)}</span></span>
        <span class="n">${p.count}<span class="visually-hidden"> reports</span></span></button>`;
      li.querySelector('button').addEventListener('click', () => {
        centreOn(L.latLng(p.lat, p.lon), 18);
        towMarkers.get(p.addr)?.openPopup();
      });
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
  function towPopup(p) {
    const types = Object.entries(p.types_en || {}).slice(0, 3)
      .map(([t, n]) => `<li>${esc(t)} — ${n}</li>`).join('');
    return `<h4>${p.count} towing report${p.count === 1 ? '' : 's'}</h4>
      <p class="ko" lang="ko">${esc(p.addr)}</p>
      <p>${monthLabel(p.first)} – ${monthLabel(p.last)} · ${p.dist_m} m from the station</p>
      <ul>${types}</ul>
      <p class="ko">Position is the lot centre, not the exact spot.</p>`;
  }
  function getCss(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

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
    const withLift = [];
    (d.exits || []).forEach((x) => {
      const cls = x.elevator === 'working' ? 'ev-working' : x.elevator === 'out of service' ? 'ev-out' : '';
      if (cls) withLift.push(x);
      const icon = L.divIcon({ className: '', html: `<div class="exit-pin ${cls}">${esc(x.ref)}</div>`, iconSize: [30, 30], iconAnchor: [15, 15] });
      const rows = (x.elevator_rows || []).map((r) => `<li lang="ko">${esc(r.name.replace(/^승강기\)/, ''))}: <strong>${esc(r.status)}</strong></li>`).join('');
      const body = x.elevator === 'none listed'
        ? '<p>No elevator is listed for this exit in the Seoul Metro feed.</p>'
        : `<p>Elevator <strong>${esc(x.elevator)}</strong></p><ul>${rows}</ul>`;
      L.marker([x.lat, x.lon], { icon, title: `Exit ${x.ref}: elevator ${x.elevator}`, keyboard: true, zIndexOffset: cls ? 500 : 0 })
        .bindPopup(`<h4>Exit ${esc(x.ref)}</h4>${body}<p class="ko">Status ${esc(d.status_source)} · ${esc(fmtTime(d.status_as_of))}</p>`)
        .addTo(exitLayer);
    });
    if (!withLift.length) {
      line.textContent = 'No exit elevators listed.';
      return;
    }
    const parts = withLift.map((x) => (x.elevator === 'working'
      ? `exit ${x.ref} working`
      : `<strong>exit ${x.ref} out of service</strong>`));
    line.innerHTML = `Elevators: ${parts.join(' · ')} <span class="asof">(${esc(d.status_source)}, ${esc(fmtTime(d.status_as_of))})</span>`;
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
    $('#analyze-btn').disabled = true;
    setStatus('Blurring faces on this server, then measuring… this can take a minute.');
    const result = await getJson('/api/analyze', { method: 'POST', body: fd });
    $('#analyze-btn').disabled = false;
    if (result) {
      useClip(result.video_url);
      showResult(result, false, at);
      setStatus(statusLine(result));
    }
  });

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

  function statusLine(r) {
    const b = r.blur || {};
    const faces = b.faces == null ? '' : ` Faces blurred before upload: ${b.faces} in ${b.frames_with_faces} of ${b.frames} frames.`;
    const cost = r.cost ? ` Paid SAM frames this check: ${r.cost.sam_frames} ($${Number(r.cost.usd_estimate || 0).toFixed(2)}).` : '';
    return `${r.sample ? 'Sample clip, replayed results (no live model calls).' : 'Done.'}${faces}${cost}`;
  }

  $('#sample-btn').addEventListener('click', async () => {
    const at = crosshairLatLng();
    if (params.get('verdict')) { await runSample(false, at); return; }  // hand-made UI fixture variants
    $('#sample-btn').disabled = true;
    setStatus('Running the sample clip…');
    const result = await getJson(`/api/sample?lat=${at.lat.toFixed(6)}&lon=${at.lng.toFixed(6)}`);
    $('#sample-btn').disabled = false;
    if (!result) return;
    clip.value = '';
    $('#analyze-btn').disabled = true;
    resetResult();
    useClip(result.video_url);
    if (!result.video_url) { canvas.width = result.frame_size[0]; canvas.height = result.frame_size[1]; }
    showResult(result, true, at);
    setStatus(statusLine(result) + (result.precomputed ? ' The clip itself is not on this server, so only the masks are shown.' : ''));
  });
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
    $('#verdict').focus({ preventScroll: false });
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

  function renderVerdict(result, sample) {
    const tri = result.triage;
    const verdict = VERDICT_WORD[result.verdict] ? result.verdict : 'unknown';
    const card = $('#verdict');
    card.dataset.verdict = verdict;
    $('#verdict-icon').setAttribute('href', `#v-${verdict}`);
    $('#verdict-text').textContent = VERDICT_WORD[verdict];
    const kicker = $('#verdict-kicker');
    kicker.textContent = result.handmade ? 'Sample · hand-made, not a measurement'
      : sample ? 'Sample clip · replayed model results' : 'Measured from your clip';
    kicker.classList.toggle('sample', sample);
    $('#v-gap').textContent = result.gap_cm == null ? '—' : `${Math.round(result.gap_cm)} cm`;
    $('#v-band').textContent = Array.isArray(result.band_cm) ? `${Math.round(result.band_cm[0])}–${Math.round(result.band_cm[1])} cm` : '—';
    $('#v-why').textContent = result.why || '';
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
    if (check.pin && !check.pin.confirmed) pinLayer.removeLayer(check.pin.marker);
    const marker = L.marker(at, { icon: pinIcon(verdict, false), draggable: true, keyboard: true, title: `Checked spot: ${VERDICT_WORD[verdict]}`, zIndexOffset: 1000 })
      .bindPopup(() => `<h4>${VERDICT_WORD[verdict]}${check.sample ? ' (sample)' : ''}</h4><p>${m.gap_cm == null ? 'No width measured' : `Gap ${Math.round(m.gap_cm)} cm`}${Array.isArray(m.band_cm) ? ` (${Math.round(m.band_cm[0])}–${Math.round(m.band_cm[1])} cm)` : ''}</p><p class="ko">${check.pin && check.pin.confirmed ? 'Confirmed' : 'Not confirmed yet. Drag to adjust.'}</p>`)
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

  // ------------------------------------------------------------------ start
  layout();
  centreOn(L.latLng(YANGJAE), 16);
  loadTowing(500);
  loadStation();
  if (params.get('check') === '1' || params.get('sample')) enterCheck();
  if (params.get('sample')) runSample(false, crosshairLatLng());
  if (params.get('open') === '1') setSheet('half');
})();

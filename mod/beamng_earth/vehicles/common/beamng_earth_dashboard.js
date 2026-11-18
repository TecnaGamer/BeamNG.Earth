(function () {
  'use strict';

  const canvas = document.getElementById('map');
  const ctx = canvas.getContext('2d', { alpha: false });
  const view = { pixelX: null, pixelY: null, zoom: null, heading: 0, attribution: '', status: '',
    x: null, y: null, north: 0, mpp: null, anchor: null };
  // The roads (the server's road graph, dashboard.lua's roadsText): drawn over the photo as lines, like the stock car
  // navigators draw a level's roads (the user, 2026-11-12: "Nav screeb blurry mess": the photo alone at 1 m a pixel
  // is a blur of 10 m Sentinel-2 imagery in most of the world, no road to follow on it). n: x, y pairs in metres from
  // (ox, oy) in the world frame of anchor; e: (node a, node b, class, half width in dm) quads.
  let roads = null;
  // stock-like look (vehicles' navi pages: grey roads with a darker edge on a grey map, navigator-class.js roadColors):
  // class 0 track, 1 service / living street, 2 town street, 3 main road
  const ROAD_FILL = ['#967864', '#a6a678', '#d8d8d8', '#f4f4f4'];
  const ROAD_MIN_PX = [1.2, 1.6, 2.2, 3.0];
  const ROAD_MAX_PX = 16;
  const CASING = 'rgba(24, 24, 24, 0.85)';
  const PHOTO_DIM = 0.5;          // the photo under the roads darkened this much (the roads read on any ground)
  const CAR_Y = 0.62;             // the car this far down the map (more road ahead; the stock pages' offsetY)
  // The tiles of the last few zoom levels (zoom -> tiles), each drawn scaled to the zoom shown: a speed change's new
  // level appears over the old one as its tiles load, and the zoom shown eases to the new level (ZOOM_TAU_S) instead
  // of jumping. Before (2026-11-08) only the newest level's tiles were kept, drawn at their own scale: the screen went
  // blank grey at every zoom step until the new tiles had loaded (the user: "it will flash grey when switching
  // between zoom levels instead of a smooth zoom out").
  const layers = new Map();
  const KEEP_LEVELS = 3;          // levels kept either side of the newest
  const ZOOM_TAU_S = 0.12;        // s: the zoom shown approaches the view's (~95 % in 0.36 s)
  let shownZoom = null;           // the zoom drawn (fractional while easing)
  let lastFrame = 0;
  let animating = false;
  let pixelRatio = 1;
  // The keys of the roads and the tiles drawn (dashboard.lua's handshake, from PR #472): each update comes with the Lua
  // the page runs back once it drew it (navDrawn, built by the mod: only echoed), and the mod's probe asks keys().
  let roadsKey = null;
  let tilesKey = null;

  function drawn(ack) {
    if (typeof ack === 'string' && ack && typeof beamng !== 'undefined' && beamng.sendEngineLua) beamng.sendEngineLua(ack);
  }

  function resize() {
    pixelRatio = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = Math.max(1, Math.round(canvas.clientWidth * pixelRatio));
    canvas.height = Math.max(1, Math.round(canvas.clientHeight * pixelRatio));
    ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    render();
  }

  function setTiles(data, ack) {
    const zoom = data && Number.isFinite(data.zoom) ? data.zoom : (view.zoom !== null ? view.zoom : 0);
    const old = new Map();
    for (const list of layers.values()) for (const tile of list) old.set(tile.src, tile.image);
    const list = [];
    for (const item of (data && Array.isArray(data.tiles) ? data.tiles : [])) {
      if (!item || typeof item.src !== 'string') continue;
      list.push({ x: item.x, y: item.y, src: item.src, image: old.get(item.src) || loadImage(item.src, 0) });
    }
    // (an empty list keeps the level's tiles: tiles not ready yet are not tiles gone)
    if (list.length || !layers.has(zoom)) layers.set(zoom, list);
    for (const z of Array.from(layers.keys())) {
      if (Math.abs(z - zoom) > KEEP_LEVELS) layers.delete(z);
    }
    render();
    tilesKey = data && typeof data.key === 'string' ? data.key : null;
    drawn(ack);
  }

  // A tile's image; one that fails to load (a file being replaced, the game's file cache not seeing it yet) is loaded
  // again after LOAD_RETRY_MS * 2^tries, LOAD_TRIES times (before, a failed image stayed in the layer for good, and
  // the mod sends a tile list only when it changes).
  const LOAD_RETRY_MS = 1000;
  const LOAD_TRIES = 5;
  function loadImage(src, tries) {
    const image = new Image();
    image.onload = render;
    image.onerror = function () {
      render();
      if (tries + 1 >= LOAD_TRIES) return;
      setTimeout(function () {
        let fresh = null;
        for (const list of layers.values()) {
          for (const tile of list) {
            if (tile.image === image) tile.image = fresh || (fresh = loadImage(src, tries + 1));
          }
        }
      }, LOAD_RETRY_MS * Math.pow(2, tries));
    };
    image.src = src;
    return image;
  }

  function frame(t) {
    animating = false;
    render(t);
  }

  // the zoom shown, a step nearer the view's (t: requestAnimationFrame's ms); true while still easing
  function easeZoom(t) {
    if (view.zoom === null) { shownZoom = null; return false; }
    const now = Number.isFinite(t) ? t : (window.performance ? performance.now() : Date.now());
    const dt = lastFrame ? Math.min(0.1, Math.max(0, (now - lastFrame) / 1000)) : 0;
    lastFrame = now;
    if (shownZoom === null || Math.abs(view.zoom - shownZoom) > KEEP_LEVELS) shownZoom = view.zoom;
    else shownZoom += (view.zoom - shownZoom) * (1 - Math.exp(-dt / ZOOM_TAU_S));
    if (Math.abs(view.zoom - shownZoom) < 0.002) shownZoom = view.zoom;
    return shownZoom !== view.zoom;
  }

  function setView(data) {
    if (!data || typeof data !== 'object') return;
    if (Number.isFinite(data.pixelX) && Number.isFinite(data.pixelY) && Number.isFinite(data.zoom)) {
      view.pixelX = data.pixelX;
      view.pixelY = data.pixelY;
      view.zoom = data.zoom;
      view.heading = Number.isFinite(data.heading) ? data.heading : 0;
      view.x = Number.isFinite(data.x) ? data.x : null;
      view.y = Number.isFinite(data.y) ? data.y : null;
      view.north = Number.isFinite(data.north) ? data.north : 0;
      view.mpp = Number.isFinite(data.mpp) && data.mpp > 0 ? data.mpp : null;
      view.anchor = data.anchor === undefined ? null : data.anchor;
    } else {
      view.pixelX = null;
      view.pixelY = null;
      view.zoom = null;
      view.x = view.y = view.mpp = null;
    }
    view.attribution = typeof data.attribution === 'string' ? data.attribution : '';
    view.status = typeof data.status === 'string' ? data.status : '';
    render();
  }

  function wrapText(text, maxWidth) {
    const words = String(text || '').replace(/\s+/g, ' ').trim().split(' ');
    if (!words[0]) return [];
    const lines = [];
    let line = words[0];
    for (let i = 1; i < words.length; i++) {
      const candidate = line + ' ' + words[i];
      if (ctx.measureText(candidate).width <= maxWidth) {
        line = candidate;
      } else {
        lines.push(line);
        line = words[i];
      }
    }
    lines.push(line);
    return lines;
  }

  function drawTiles(centerX, centerY, mapHeight) {
    if (view.pixelX === null || view.pixelY === null || view.zoom === null || shownZoom === null) return 0;
    let drawn = 0;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, canvas.clientWidth, mapHeight);
    ctx.clip();
    ctx.translate(centerX, centerY);
    ctx.rotate(-view.heading * Math.PI / 180);
    ctx.translate(-centerX, -centerY);
    // the car's place at the zoom shown (view.pixelX/Y are at the view's whole zoom)
    const k = Math.pow(2, shownZoom - view.zoom);
    const cx = view.pixelX * k;
    const cy = view.pixelY * k;
    // the farthest level first, the level nearest the zoom shown on top
    const order = Array.from(layers.keys()).sort((a, b) => Math.abs(b - shownZoom) - Math.abs(a - shownZoom));
    for (const z of order) {
      const size = 256 * Math.pow(2, shownZoom - z);
      for (const tile of layers.get(z)) {
        const image = tile.image;
        if (!image || !image.complete || !image.naturalWidth) continue;
        const x = centerX + tile.x * size - cx;
        const y = centerY + tile.y * size - cy;
        // (a hair larger: no seams between scaled tiles)
        ctx.drawImage(image, x, y, size + 0.5, size + 0.5);
        drawn++;
      }
    }
    ctx.restore();
    return drawn;
  }

  function setRoads(data, ack) {
    if (!data || typeof data !== 'object' || !Array.isArray(data.n) || !Array.isArray(data.e)) {
      roads = null;
    } else {
      roads = { key: data.key, anchor: data.anchor, ox: +data.ox || 0, oy: +data.oy || 0,
        n: Float64Array.from(data.n), e: Int32Array.from(data.e) };
    }
    roadsKey = data && typeof data === 'object' && typeof data.key === 'string' ? data.key : null;
    render();
    drawn(ack);
  }

  function keys() {
    return { roads: roadsKey, tiles: tilesKey };
  }

  // The roads round the car, in the photo's pixels at the zoom shown: world metres from the car turned to true
  // east / north (view.north: true north's angle in the world frame at the car), then over the metres a pixel.
  function drawRoads(centerX, centerY, mapHeight, width) {
    if (!roads || view.x === null || view.mpp === null || shownZoom === null || roads.anchor !== view.anchor) return 0;
    const k = Math.pow(2, shownZoom - view.zoom) / view.mpp;       // pixels a metre
    const c = Math.cos(view.north), s = Math.sin(view.north);
    const n = roads.n, e = roads.e;
    const count = n.length >> 1;
    const px = new Float32Array(count), py = new Float32Array(count);
    const dx0 = roads.ox - view.x, dy0 = roads.oy - view.y;
    for (let i = 0; i < count; i++) {
      const dx = dx0 + n[2 * i], dy = dy0 + n[2 * i + 1];
      px[i] = (dx * c - dy * s) * k;
      py[i] = -(dx * s + dy * c) * k;
    }
    const R = Math.hypot(Math.max(centerX, width - centerX), Math.max(centerY, mapHeight - centerY)) + ROAD_MAX_PX;
    // the edges by class and width (one path each: a few strokes a frame, not one a road)
    const groups = new Map();
    for (let j = 0; j + 3 < e.length; j += 4) {
      const a = e[j], b = e[j + 1];
      const ax = px[a], ay = py[a], bx = px[b], by = py[b];
      if ((ax < -R && bx < -R) || (ax > R && bx > R) || (ay < -R && by < -R) || (ay > R && by > R)) continue;
      const cls = Math.max(0, Math.min(3, e[j + 2]));
      const w = Math.round(2 * Math.min(ROAD_MAX_PX, Math.max(ROAD_MIN_PX[cls], 0.2 * e[j + 3] * k))) / 2;
      const key = cls * 1000 + w * 10;
      let g = groups.get(key);
      if (!g) { g = { cls: cls, w: w, list: [] }; groups.set(key, g); }
      g.list.push(ax, ay, bx, by);
    }
    if (!groups.size) return 0;
    const order = Array.from(groups.values()).sort((A, B) => A.cls - B.cls || A.w - B.w);
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, width, mapHeight);
    ctx.clip();
    ctx.translate(centerX, centerY);
    ctx.rotate(-view.heading * Math.PI / 180);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    const stroke = (g, colour, extra) => {
      ctx.beginPath();
      const L = g.list;
      for (let i = 0; i < L.length; i += 4) { ctx.moveTo(L[i], L[i + 1]); ctx.lineTo(L[i + 2], L[i + 3]); }
      ctx.lineWidth = g.w + extra;
      ctx.strokeStyle = colour;
      ctx.stroke();
    };
    for (const g of order) stroke(g, CASING, 2);          // every edge first: junctions meet without dark seams
    for (const g of order) stroke(g, ROAD_FILL[g.cls], 0);
    ctx.restore();
    return order.length;
  }

  function drawPlayer(centerX, centerY) {
    ctx.save();
    ctx.translate(centerX, centerY);
    // (the stock navi pages' marker: a blue arrow with a dark shadow, ~30 px on a 512 x 256 screen)
    const arrow = (dy, colour) => {
      ctx.beginPath();
      ctx.moveTo(0, -11 + dy);
      ctx.lineTo(8, 9 + dy);
      ctx.lineTo(0, 5 + dy);
      ctx.lineTo(-8, 9 + dy);
      ctx.closePath();
      ctx.fillStyle = colour;
      ctx.fill();
    };
    arrow(2, '#282828');
    arrow(0, '#3570c4');
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.9)';
    ctx.stroke();
    ctx.restore();
  }

  // text cut to maxWidth with an ellipsis
  function fit(text, maxWidth) {
    if (ctx.measureText(text).width <= maxWidth) return text;
    let lo = 0, hi = text.length;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (ctx.measureText(text.slice(0, mid) + '…').width <= maxWidth) lo = mid; else hi = mid - 1;
    }
    return text.slice(0, lo).trimEnd() + '…';
  }

  function render(t) {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    if (easeZoom(t) && !animating) {
      animating = true;
      window.requestAnimationFrame(frame);
    }
    ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    ctx.fillStyle = '#3a3a3a';
    ctx.fillRect(0, 0, width, height);

    // sizes for the vehicles' screen textures (512 x 256 on most cars: the old 4 px font was a grey smear, 2026-11-12)
    const fontPx = Math.max(8, Math.round(height / 26));
    ctx.font = fontPx + 'px sans-serif';
    const centerX = width / 2;
    let status = view.status || (view.pixelX === null ? 'Waiting for satellite map' : '');
    const haveRoads = !!(roads && roads.anchor === view.anchor && view.x !== null);
    // (a zoom step's new tiles still loading while the last level's or the roads show: no status bar flashing over them)
    if (/loading/i.test(status) && (haveRoads || layers.size > 1)) status = '';
    const statusHeight = status ? fontPx + 4 : 0;
    const mapHeight = height;
    const centerY = Math.round(mapHeight * CAR_Y);
    const drawn = drawTiles(centerX, centerY, mapHeight);
    if (drawn && haveRoads) {
      ctx.fillStyle = 'rgba(0, 0, 0, ' + PHOTO_DIM + ')';
      ctx.fillRect(0, 0, width, mapHeight);
    }
    const roadGroups = drawRoads(centerX, centerY, mapHeight, width);
    if (view.pixelX !== null) drawPlayer(centerX, centerY);

    if (status) {
      ctx.fillStyle = 'rgba(0, 0, 0, 0.68)';
      ctx.fillRect(0, 0, width, statusHeight);
      ctx.fillStyle = '#fff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(fit(status, width - 8), centerX, statusHeight / 2);
    } else if (!drawn && !roadGroups) {
      ctx.fillStyle = 'rgba(0, 0, 0, 0.55)';
      ctx.fillRect(0, 0, width, fontPx + 4);
      ctx.fillStyle = '#fff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('Satellite tiles loading', centerX, (fontPx + 4) / 2);
    }

    // the imagery credit: one or two readable lines at the bottom (the licences ask for it on screen)
    if (view.attribution && drawn) {
      const small = Math.max(7, fontPx - 2);
      const lineHeight = small + 2;
      const pad = Math.max(6, Math.round(width * 0.05));     // (the screens' bezels hide ~4 % of a side: vivace)
      ctx.font = small + 'px sans-serif';
      let lines = wrapText(view.attribution, Math.max(1, width - 2 * pad));
      if (lines.length > 2) lines = [lines[0], fit(lines.slice(1).join(' '), width - 2 * pad)];
      const footerHeight = lines.length * lineHeight + 2;
      ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
      ctx.fillRect(0, height - footerHeight, width, footerHeight);
      ctx.fillStyle = '#e8e8e8';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      lines.forEach((line, index) => {
        ctx.fillText(line, pad, height - footerHeight + 1 + index * lineHeight);
      });
    }
  }

  function restore() {
    const query = new URLSearchParams(window.location.search);
    const original = query.get('return');
    // only a vehicle screen's own page (dashboard.lua sends its local:// href): never a javascript: or remote URL
    // (GitHub code scanning #1, #4)
    if (original && /^local:\/\//i.test(original)) window.location.replace(original);
  }

  // ui_uinavi still sends stock map updates after registration; this page intentionally uses only
  // the companion's satellite tiles and does not replace or alter any vehicle/navigation data.
  window.map = {
    setData: function () {},
    updateData: function () {},
    setBootscreenImage: function () {}
  };
  window.beamngEarthDashboard = { setTiles: setTiles, setView: setView, setRoads: setRoads, restore: restore, keys: keys };
  window.addEventListener('resize', resize);
  resize();
  const params = new URLSearchParams(window.location.search);
  const vehicleId = Number(params.get('vehicle'));
  const screen = params.get('screen');
  if (Number.isInteger(vehicleId) && screen && typeof beamng !== 'undefined' && beamng.sendEngineLua) {
    beamng.sendEngineLua('if beamngEarth_dashboard then beamngEarth_dashboard.screenReady('
      + vehicleId + ',' + JSON.stringify(screen) + ') end');
  }
}());

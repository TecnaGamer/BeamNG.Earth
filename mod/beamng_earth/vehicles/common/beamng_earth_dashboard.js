(function () {
  'use strict';

  const canvas = document.getElementById('map');
  const ctx = canvas.getContext('2d', { alpha: false });
  const view = { pixelX: null, pixelY: null, zoom: null, heading: 0, attribution: '', status: '' };
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

  function resize() {
    pixelRatio = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = Math.max(1, Math.round(canvas.clientWidth * pixelRatio));
    canvas.height = Math.max(1, Math.round(canvas.clientHeight * pixelRatio));
    ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    render();
  }

  function setTiles(data) {
    const zoom = data && Number.isFinite(data.zoom) ? data.zoom : (view.zoom !== null ? view.zoom : 0);
    const old = new Map();
    for (const list of layers.values()) for (const tile of list) old.set(tile.src, tile.image);
    const list = [];
    for (const item of (data && Array.isArray(data.tiles) ? data.tiles : [])) {
      if (!item || typeof item.src !== 'string') continue;
      const image = old.get(item.src) || new Image();
      if (!image.src) {
        image.onload = render;
        image.onerror = render;
        image.src = item.src;
      }
      list.push({ x: item.x, y: item.y, src: item.src, image: image });
    }
    // (an empty list keeps the level's tiles: tiles not ready yet are not tiles gone)
    if (list.length || !layers.has(zoom)) layers.set(zoom, list);
    for (const z of Array.from(layers.keys())) {
      if (Math.abs(z - zoom) > KEEP_LEVELS) layers.delete(z);
    }
    render();
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
    } else {
      view.pixelX = null;
      view.pixelY = null;
      view.zoom = null;
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

  function drawPlayer(centerX, centerY) {
    ctx.save();
    ctx.translate(centerX, centerY);
    ctx.fillStyle = '#fff';
    ctx.strokeStyle = '#111';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(0, -6);
    ctx.lineTo(4.5, 4.5);
    ctx.lineTo(0, 2.5);
    ctx.lineTo(-4.5, 4.5);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.restore();
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
    ctx.fillStyle = '#111';
    ctx.fillRect(0, 0, width, height);

    ctx.font = '6px sans-serif';
    const centerX = width / 2;
    let status = view.status || (view.pixelX === null ? 'Waiting for satellite map' : '');
    let statusHeight = status ? 10 : 0;
    let mapHeight = Math.max(1, height - statusHeight);
    let centerY = mapHeight / 2;
    let drawn = drawTiles(centerX, centerY, mapHeight);
    if (drawn && /loading/i.test(status)) {
      // (a zoom step's new tiles still loading while the last level's show: no status bar flashing over them)
      status = '';
      statusHeight = 0;
      mapHeight = height;
      centerY = mapHeight / 2;
      ctx.fillStyle = '#111';
      ctx.fillRect(0, 0, width, height);
      drawn = drawTiles(centerX, centerY, mapHeight);
    }
    if (view.pixelX !== null) drawPlayer(centerX, centerY);

    if (status) {
      ctx.fillStyle = 'rgba(0, 0, 0, 0.68)';
      ctx.fillRect(0, 0, width, statusHeight);
      ctx.fillStyle = '#fff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(status, centerX, statusHeight / 2, width - 6);
    } else if (!drawn) {
      ctx.fillStyle = 'rgba(0, 0, 0, 0.55)';
      ctx.fillRect(2, mapHeight - 11, width - 4, 9);
      ctx.fillStyle = '#fff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('Satellite tiles loading', centerX, mapHeight - 6.5, width - 8);
    }

    if (view.attribution) {
      const fontSize = 4;
      const lineHeight = 5;
      const pad = 2;
      ctx.font = fontSize + 'px sans-serif';
      const lines = wrapText(view.attribution, Math.max(1, width - 2 * pad));
      const footerHeight = Math.min(height, lines.length * lineHeight + pad * 2);
      ctx.fillStyle = 'rgba(0, 0, 0, 0.76)';
      ctx.fillRect(0, height - footerHeight, width, footerHeight);
      ctx.fillStyle = '#fff';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      lines.forEach((line, index) => {
        ctx.fillText(line, pad, height - footerHeight + pad + index * lineHeight);
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
  window.beamngEarthDashboard = { setTiles: setTiles, setView: setView, restore: restore };
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

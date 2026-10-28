(function () {
  'use strict';

  const canvas = document.getElementById('map');
  const ctx = canvas.getContext('2d', { alpha: false });
  const view = { pixelX: null, pixelY: null, zoom: null, heading: 0, attribution: '', status: '' };
  let tiles = [];
  let pixelRatio = 1;

  function resize() {
    pixelRatio = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = Math.max(1, Math.round(canvas.clientWidth * pixelRatio));
    canvas.height = Math.max(1, Math.round(canvas.clientHeight * pixelRatio));
    ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    render();
  }

  function setTiles(data) {
    const old = new Map(tiles.map((tile) => [tile.src, tile.image]));
    tiles = [];
    for (const item of (data && Array.isArray(data.tiles) ? data.tiles : [])) {
      if (!item || typeof item.src !== 'string') continue;
      const image = old.get(item.src) || new Image();
      if (!image.src) {
        image.onload = render;
        image.onerror = render;
        image.src = item.src;
      }
      tiles.push({ x: item.x, y: item.y, src: item.src, image: image });
    }
    render();
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
    if (view.pixelX === null || view.pixelY === null || view.zoom === null) return 0;
    let drawn = 0;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, canvas.clientWidth, mapHeight);
    ctx.clip();
    ctx.translate(centerX, centerY);
    ctx.rotate(-view.heading * Math.PI / 180);
    ctx.translate(-centerX, -centerY);
    for (const tile of tiles) {
      const image = tile.image;
      if (!image || !image.complete || !image.naturalWidth) continue;
      const x = centerX + tile.x * 256 - view.pixelX;
      const y = centerY + tile.y * 256 - view.pixelY;
      ctx.drawImage(image, x, y, 256, 256);
      drawn++;
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

  function render() {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    ctx.fillStyle = '#111';
    ctx.fillRect(0, 0, width, height);

    ctx.font = '6px sans-serif';
    const status = view.status || (view.pixelX === null ? 'Waiting for satellite map' : '');
    const statusHeight = status ? 10 : 0;
    const centerX = width / 2;
    const mapHeight = Math.max(1, height - statusHeight);
    const centerY = mapHeight / 2;
    const drawn = drawTiles(centerX, centerY, mapHeight);
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
    if (original) window.location.replace(original);
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

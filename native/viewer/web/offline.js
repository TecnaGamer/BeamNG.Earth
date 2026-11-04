// The guide sign shown while the world server (earthd) can't be reached, with the way to start it:
// POST /api/earthd/start (launch.rs: run_server.bat / run_server.sh in a window of its own), then
// quick reconnects until earthd answers. Also drives the panel's connection pill.
const $ = id => document.getElementById(id)

// a first start installs Python and its packages: after this long, say so
const SLOW_S = 60

export class Offline {
  constructor(net, config) {
    this.net = net
    this.cfg = config || {}
    this.state = 'checking'      // checking | live | offline | starting | failed
    this.startedAt = 0
    this.wasLive = false
    this.error = ''
    this.shown = ''
    this.el = $('offline')
    this.plate = this.el.querySelector('.plate')
    this.btn = $('offStart')
    this.retry = $('offRetry')
    $('offAddr').textContent = this.cfg.earthd || '127.0.0.1:47613'
    this.btn.onclick = () => this.start()
    this.retry.onclick = () => {
      this.net.retryNow()
      this.retry.disabled = true
      this.retry.textContent = 'Trying'
      setTimeout(() => { this.retry.disabled = false; this.shown = ''; this.render() }, 1200)
    }
    this.render()
  }

  script() { return this.cfg.serverScript || 'run_server.bat' }

  // from the frame loop (a few times a second)
  update() {
    const net = this.net
    let s = this.state
    if (net.connected) s = 'live'
    else if (!net.tried) s = 'checking'
    else if (s === 'live' || s === 'checking') s = 'offline'
    if (s === 'live') { this.wasLive = true; net.fast = false }
    if (s !== this.state || s === 'starting') { this.state = s; this.render() }
  }

  async start() {
    this.state = 'starting'
    this.startedAt = performance.now()
    this.error = ''
    this.render()
    try {
      const r = await fetch('/api/earthd/start', { method: 'POST' })
      const j = await r.json().catch(() => ({}))
      if (!r.ok || !j.ok) throw new Error(j.error || `the viewer answered HTTP ${r.status}`)
      this.net.fast = true
      this.net.retryNow()
    } catch (e) {
      this.state = 'failed'
      this.error = e.message === 'Failed to fetch' ? 'The viewer program did not answer.' : e.message
      this.render()
    }
  }

  render() {
    const s = this.state, net = this.net, pill = $('conn')
    const down = s === 'offline' || s === 'failed'
    pill.dataset.state = down ? 'offline' : s
    pill.textContent = { checking: 'Checking', live: 'Live', offline: 'Server off', starting: 'Starting', failed: 'Server off' }[s]
    pill.title = s === 'live' ? `World server at ${this.cfg.earthd || '127.0.0.1:47613'}` : net.lastError

    const show = s !== 'live' && s !== 'checking'
    this.el.classList.toggle('hidden', !show)
    if (!show) { this.shown = ''; return }
    this.plate.dataset.state = s
    const title = $('offTitle'), body = $('offBody'), hint = $('offHint')
    const canStart = this.cfg.canStart === true
    const detail = (text) => { $('offDetail').classList.toggle('hidden', !text); $('offError').textContent = text || '' }
    // (move the focus to the sign's action when it first shows, for keyboards and screen readers)
    const focus = () => { if (this.shown !== s) { this.shown = s; (this.btn.offsetParent ? this.btn : this.retry).focus({ preventScroll: true }) } }

    if (!this.retry.disabled) this.retry.textContent = 'Try again'
    this.btn.disabled = s === 'starting'

    if (net.viewerGone) {
      title.textContent = 'Viewer program closed'
      body.textContent = 'This page is served by the viewer program, and it has stopped.'
      hint.innerHTML = 'Run <code>run_viewer.bat</code> again. This page reconnects by itself.'
      this.btn.classList.add('hidden')
      detail(net.lastError)
      return focus()
    }
    this.btn.classList.toggle('hidden', !canStart)

    if (s === 'starting') {
      const t = Math.round((performance.now() - this.startedAt) / 1000)
      title.textContent = 'Starting the world server'
      this.btn.textContent = `Starting, ${t} s`
      body.textContent = t < SLOW_S
        ? 'A window opened for the server. Keep it open while you use the viewer: closing it stops the server.'
        : 'Still starting. The first start installs Python packages and can take a few minutes. Check the server window for a question or an error.'
      hint.textContent = 'This page connects by itself once the server answers.'
      detail('')
      return focus()
    }
    this.btn.textContent = 'Start the world server'
    if (s === 'failed') {
      title.textContent = "The world server didn't start"
      body.textContent = this.error
      hint.innerHTML = `Start it yourself: double-click <code>${this.script()}</code> in the BeamNG.Earth folder, then try again.`
      detail('')
      return focus()
    }
    title.textContent = this.wasLive ? 'World server stopped' : 'World server not running'
    body.textContent = this.wasLive
      ? 'The connection to the world server closed, so the view is empty until it is back.'
      : 'The viewer shows what the world server builds for the game, so it needs the server running.'
    if (canStart) hint.innerHTML = `This runs <code>${this.script()}</code> in a new window. The game doesn't need to be open.`
    else if (this.cfg.earthd && !/^(127\.|localhost|\[::1\])/.test(this.cfg.earthd)) hint.textContent = `The viewer is set to a server on another computer, ${this.cfg.earthd}. Start it there, then try again.`
    else hint.innerHTML = `Start it with <code>${this.script()}</code> in the BeamNG.Earth folder, then try again.`
    detail(net.lastError)
    focus()
  }
}

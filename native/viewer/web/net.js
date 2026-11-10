// The connection to earthd through the viewer's /ws bridge (bridge.rs): JSON messages both ways,
// `_connected` / `_disconnected` from the bridge like the mod's net.lua. Reconnects every 3 s (every
// second while the page waits for a server it has just started: `fast`), or at once on retryNow().
export class Net {
  constructor(onMessage) {
    this.onMessage = onMessage
    this.ws = null
    this.connected = false
    this.tried = false          // an attempt has finished (connected or not): the page knows earthd's state
    this.lastError = ''
    this.viewerGone = false     // the viewer program itself didn't answer (no bridge note came)
    this.fast = false
    this.timer = null
    this.sent = 0
    this.received = 0
    this.open()
  }
  open() {
    clearTimeout(this.timer)
    this.timer = null
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`)
    this.ws = ws
    let noted = false
    ws.onmessage = ev => {
      this.received++
      let msg
      try { msg = JSON.parse(ev.data) } catch (e) { console.warn('bad message', e); return }
      if (msg.type === '_connected') { this.connected = true; this.tried = true; this.lastError = ''; this.viewerGone = false; noted = true }
      if (msg.type === '_disconnected') { this.connected = false; this.tried = true; this.lastError = msg.error || ''; this.viewerGone = false; noted = true }
      try { this.onMessage(msg) } catch (e) { console.error('message handler failed:', String(msg.type), e) }   // (no format string from the message: code scanning #3)
    }
    ws.onclose = () => {
      if (this.ws !== ws) return
      if (this.connected) {
        this.connected = false
        this.onMessage({ type: '_disconnected', error: 'viewer connection closed' })
      } else if (!noted) {
        this.tried = true
        this.viewerGone = true
        this.lastError = `nothing answered at ${location.host}`
      }
      this.timer = setTimeout(() => this.open(), this.fast ? 1000 : 3000)
    }
  }
  // a new attempt now (unless one is open or under way)
  retryNow() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return
    this.open()
  }
  send(msg) {
    if (!this.connected || this.ws.readyState !== WebSocket.OPEN) return false
    this.ws.send(JSON.stringify(msg))
    this.sent++
    return true
  }
}

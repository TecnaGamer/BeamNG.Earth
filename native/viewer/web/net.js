// The connection to earthd through the viewer's /ws bridge (bridge.rs): JSON messages both ways,
// `_connected` / `_disconnected` from the bridge like the mod's net.lua. Reconnects every 3 s.
export class Net {
  constructor(onMessage) {
    this.onMessage = onMessage
    this.ws = null
    this.connected = false
    this.sent = 0
    this.received = 0
    this.open()
  }
  open() {
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`)
    this.ws = ws
    ws.onmessage = ev => {
      this.received++
      let msg
      try { msg = JSON.parse(ev.data) } catch (e) { console.warn('bad message', e); return }
      if (msg.type === '_connected') this.connected = true
      if (msg.type === '_disconnected') this.connected = false
      try { this.onMessage(msg) } catch (e) { console.error(msg.type, e) }
    }
    ws.onclose = () => {
      if (this.connected) {
        this.connected = false
        this.onMessage({ type: '_disconnected', error: 'viewer connection closed' })
      }
      setTimeout(() => this.open(), 3000)
    }
  }
  send(msg) {
    if (!this.connected || this.ws.readyState !== WebSocket.OPEN) return false
    this.ws.send(JSON.stringify(msg))
    this.sent++
    return true
  }
}

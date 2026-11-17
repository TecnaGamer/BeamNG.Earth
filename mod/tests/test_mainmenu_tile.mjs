// node --test mod/tests/test_mainmenu_tile.mjs: mainMenuTile.js, BeamNG.Earth's tile in Quick-Start's place
import test from "node:test"
import assert from "node:assert/strict"

function el(cls = "", kids = []) {
  const e = {
    classSet: new Set(cls.split(" ").filter(Boolean)), kids, attrs: {}, styles: {}, textContent: "", listeners: {}, id: "",
    get className() { return [...this.classSet].join(" ") }, set className(v) { this.classSet = new Set(v.split(" ")) },
    classList: null,
    style: null,
    setAttribute(k, v) { this.attrs[k] = v }, removeAttribute(k) { delete this.attrs[k] },
    addEventListener(t, f) { (this.listeners[t] ||= []).push(f) },
    remove() { this.removed = true },
    prepend(c) { this.kids.unshift(c) },
    all() { return this.kids.flatMap(k => [k, ...k.all()]) },
    querySelectorAll(sel) { return this.all().filter(k => match(k, sel)) },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null },
    cloneNode() { const c = el(this.className, this.kids.map(k => k.cloneNode())); c.attrs = { ...this.attrs }; c.textContent = this.textContent; return c },
  }
  e.classList = { add: c => e.classSet.add(c), remove: c => e.classSet.delete(c), contains: c => e.classSet.has(c) }
  e.style = { setProperty: (k, v) => { e.styles[k] = v }, removeProperty: k => { delete e.styles[k] } }
  return e
}
function match(k, sel) {
  const last = sel.trim().split(/\s+/).pop()
  return last.split(".").filter(Boolean).every(c => k.classSet.has(c))
}
globalThis.window = {}
globalThis.document = { createElement: () => el() }
const T = await import("../beamng_earth/ui/ui-vue/mods/beamngEarth/mainMenuTile.js")

function stockTile() {
  const icon = el("icon-inline"); icon.textContent = "K"
  const text = el("text heading"); text.textContent = "Quick-Start Experiences"
  const t = el("mainmenu-button huge2 tile tile-experiences has-image",
    [el("artwork", [el("zoom artwork-image")]), el("label-container", [icon, text]), el("tag")])
  t.setAttribute("bng-scoped-nav-autofocus", "force")
  return t
}

test("the tile is the stock one made Earth's: title, globe, picture, no New tag", () => {
  const t = T.makeTile(stockTile())
  assert.equal(t.id, T.TILE_ID)
  assert.equal(t.querySelector(".label-container .text").textContent, "BeamNG.Earth")
  assert.equal(t.querySelector(".icon-inline").textContent, "\uECA2")
  assert.equal(t.styles["--artwork-image"], `url('${T.PREVIEW}')`)
  assert.ok(t.kids.find(k => k.classSet.has("tag")).removed)
  assert.ok(!t.classList.contains("tile-experiences"))
  assert.ok(!("bng-scoped-nav-autofocus" in t.attrs))
  assert.ok(t.listeners.click && t.listeners.click.length === 1)
})

test("a click starts the level list's own freeroam start at Where you left off", () => {
  const sent = []
  window.bngApi = { engineLua: s => sent.push(s) }
  T.makeTile(stockTile()).listeners.click[0]()
  assert.deepEqual(sent.slice(0, 1), ['freeroam_freeroam.startFreeroamByName("beamng_earth", "spawn_default")'])
})

test("menu routes only", () => {
  assert.ok(T.isMenuRoute({ name: "menu" }))
  assert.ok(!T.isMenuRoute({ name: "play" }))
  assert.ok(!T.isMenuRoute(null))
  assert.match(T.SERVER_LUA, /n\.PORT/)         // (net.lua's port, -bngeport included)
  assert.match(T.SETTING_LUA, /mainMenuTile/)    // (the Interface setting; on unless it is off)
  assert.match(T.SERVER_LUA, /'up' or 'down'/)
})

test("a setting reply after stopTile installs nothing (Codex review: lifecycle generation)", async () => {
  const pending = []
  let observers = 0, intervals = 0
  const saved = { mo: globalThis.MutationObserver, si: globalThis.setInterval, qs: globalThis.document.querySelector }
  globalThis.MutationObserver = class { observe() { observers++ } disconnect() {} }
  globalThis.setInterval = () => { intervals++; return 1 }
  globalThis.document.querySelector = () => null
  globalThis.document.querySelectorAll = () => []
  window.vueRouter = { currentRoute: { value: { name: "menu" } } }
  window.Vue = { watch: (src, cb) => { cb(src.value); return () => {} } }
  window.bngApi = { engineLua: (code, cb) => { if (cb) pending.push(cb) } }
  try {
    await T.startTile()
    assert.equal(pending.length, 1, "the setting asked once on the menu")
    T.stopTile()
    pending.splice(0).forEach(cb => cb("on"))
    assert.equal(observers, 0, "no observer after stopTile")
    assert.equal(intervals, 0, "no server poll after stopTile")
    await T.startTile()
    pending.splice(0).forEach(cb => cb("on"))
    assert.equal(observers, 1, "a live reply shows the tile's watcher")
    T.stopTile()
  } finally {
    globalThis.MutationObserver = saved.mo; globalThis.setInterval = saved.si; globalThis.document.querySelector = saved.qs
    delete window.vueRouter; delete window.Vue; delete window.bngApi
  }
})

// node mod/tests/test_restart_box.mjs  (earthRestart.js: the settings page's "Restart world server" box, 2026-11-15)
// Hidden while nothing waits; the button with the settings' names when one does and the server can restart; no button
// for a server that cannot (it was not started by the app) or while a restart is under way; the BeamMP note; the done
// and failed lines; Lua's empty table ({}) read as no settings.
import assert from "node:assert/strict"
import { restartView } from "../beamng_earth/ui/ui-vue/mods/beamngEarth/earthRestart.js"

const names = { expAiTunnels: "Traffic through tunnels", groundGrid: "Ground grid", maxRamGb: "Max RAM for BeamNG.Earth" }
const nameOf = id => names[id]

let v = restartView(undefined, nameOf)
assert.equal(v.show, false, "no state (an older mod's Lua): hidden")
v = restartView({ pending: {}, restartable: true }, nameOf)
assert.equal(v.show, false, "nothing waits (Lua's empty table): hidden")

v = restartView({ pending: ["expAiTunnels", "groundGrid"], restartable: true }, nameOf)
assert.ok(v.show && v.button && !v.busy, "a setting waits: the button")
assert.match(v.text, /Traffic through tunnels, Ground grid/, "named as on the page")
assert.match(v.note, /game stays open/)
assert.match(v.note, /car is held still/)
assert.doesNotMatch(v.note, /multiplayer/, "no BeamMP note in single player")

v = restartView({ pending: ["maxRamGb"], restartable: true, mp: true }, nameOf)
assert.ok(v.button && /Max RAM/.test(v.text) && /multiplayer only your own world reloads/.test(v.note), "BeamMP: said")

v = restartView({ pending: ["expAiTunnels"], restartable: false }, nameOf)
assert.ok(v.show && !v.button && /Restart BeamNG.Earth to apply: Traffic through tunnels/.test(v.text),
  "a server without a supervisor: no button, what to do instead")

v = restartView({ pending: ["expAiTunnels"], restartable: false, busy: true, phase: "starting",
  text: "Restarting the world server: starting it again" }, nameOf)
assert.ok(v.show && v.busy && !v.button && v.text.startsWith("Restarting"), "under way: its line, no button")

v = restartView({ pending: [], restartable: true, phase: "done", text: "World server restarted" }, nameOf)
assert.ok(v.show && !v.button && v.level === "ok", "done: said")

v = restartView({ pending: ["expAiTunnels"], restartable: true, phase: "failed", text: "World server restart: no answer" }, nameOf)
assert.ok(v.show && v.button && v.level === "warn" && v.text === "World server restart: no answer", "failed: why, and try again")
v = restartView({ pending: [], restartable: true, phase: "failed", text: "World server restart: refused" }, nameOf)
assert.ok(v.show && !v.button && v.level === "warn", "failed with nothing waiting: why, no button")

v = restartView({ pending: {}, restartable: true }, nameOf, ["Download cache folder"])
assert.ok(v.button && /Download cache folder/.test(v.text), "the cache folder chosen for the next start: a restart takes it")

v = restartView({ pending: ["somethingNew"], restartable: true }, nameOf)
assert.match(v.text, /somethingNew/, "a setting the page does not name: its id")

console.log("test_restart_box: ok")

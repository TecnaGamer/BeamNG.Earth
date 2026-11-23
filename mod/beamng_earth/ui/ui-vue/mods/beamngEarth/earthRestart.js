// The settings page's "Restart world server" box (EarthSettingsBody.vue, earthSettingsStore.js): what it says and
// whether its button shows, from the Lua side's status.restart (core.restartState: the settings waiting for a restart,
// the button's availability, a restart under way; srvrestart.lua) and the settings' names. Pure: no Vue, no game.

const asList = v => (Array.isArray(v) ? v : (v && typeof v === "object" ? Object.values(v) : []))  // (Lua's {} is an object)

// r: status.restart (or nothing); nameOf: setting id -> its name on the page; more: names of other things a restart
// applies (the download cache folder picked for the "next start").
// Returns { show, busy, button, level ('info' | 'warn' | 'ok'), text, note }.
export function restartView(r, nameOf, more, t) {
  const hidden = { show: false, busy: false, button: false, level: "info", text: "", note: "" }
  if (!r || typeof r !== "object") return hidden
  const pending = asList(r.pending).filter(id => typeof id === "string")
  const names = pending.map(id => (nameOf && nameOf(id)) || id).concat(asList(more).filter(n => typeof n === "string"))
  const text = (key, fallback, params) => t ? t(key, params) : fallback
  const mpNote = r.mp ? text("restart.multiplayer", " In multiplayer only your own world reloads: the other players drive on, your car waits.") : ""
  if (r.busy) {
    return { show: true, busy: true, button: false, level: "info", text: r.text || "Restarting the world server ...",
      note: text("restart.reloadNotice", "The world reloads around you; the game stays open.") + mpNote }
  }
  if (r.phase === "done") {
    return { show: true, busy: false, button: false, level: "ok", text: r.text || "World server restarted", note: "" }
  }
  const failed = r.phase === "failed" ? (r.text || "World server restart: it did not work") : ""
  if (!names.length) {
    return failed ? { ...hidden, show: true, level: "warn", text: failed } : hidden
  }
  const list = names.join(", ")
  if (!r.restartable) {
    return { show: true, busy: false, button: false, level: failed ? "warn" : "info",
      text: failed || text("restart.appRequired", `Restart BeamNG.Earth to apply: ${list}.`, { settings: list }),
      note: failed ? text("restart.waiting", `Waiting for a restart: ${list}.`, { settings: list })
        : text("restart.externalServer", "This world server cannot restart itself (it was not started by the BeamNG.Earth app).") }
  }
  return { show: true, busy: false, button: true, level: failed ? "warn" : "info",
    text: failed || text("restart.serverRequired", `Waiting for a world server restart: ${list}.`, { settings: list }),
    note: text("restart.whatHappens", "Restart world server: the world reloads around you in a few seconds (up to a minute on a slower PC), the "
      + "game stays open and your car is held still until the ground and roads near it are back.") + mpNote }
}

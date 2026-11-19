// Pasting into BeamNG.Earth's text boxes (the user, 2026-11-14: "make sure all text boxes (like the custom add ons and
// map search) supports pasting"). The game's embedded browser has no clipboard of its own: Ctrl+V did nothing in them.
// The text comes from the engine instead (Lua getClipboard(), as the stock multiplayer join box's paste button does)
// and goes in at the cursor, replacing the selection, within the box's maxlength, one line for an <input>.
// One capturing keydown listener on the document (index.js onLoad / onUnload), for the mod's own UI only (ROOTS): the
// stock UI's boxes are left as they are.

export const ROOTS = ".earth-map, .earth-map3d, .addons, .earth-welcome, .earth-main, .earth-side"
const TEXT_TYPES = new Set(["", "text", "search", "url", "email", "tel", "password", "number"])
const KEY = Symbol.for("beamngEarth.paste")

// Is this keydown a paste (Ctrl+V / Cmd+V, Shift+Insert)?
export function isPaste(e) {
  if (!e) return false
  const v = e.key === "v" || e.key === "V" || e.code === "KeyV"
  return ((e.ctrlKey || e.metaKey) && !e.altKey && v) || (e.shiftKey && !e.ctrlKey && e.key === "Insert")
}

// Is el a text box this handles (an enabled, writable text <input> or <textarea> in the mod's UI)?
export function isTextBox(el) {
  if (!el || el.disabled || el.readOnly) return false
  const tag = (el.tagName || "").toUpperCase()
  if (tag === "TEXTAREA") return !!(el.closest && el.closest(ROOTS))
  if (tag !== "INPUT" || !TEXT_TYPES.has(String(el.type || "").toLowerCase())) return false
  return !!(el.closest && el.closest(ROOTS))
}

function selection(el) {
  let s = null, e = null
  try { s = el.selectionStart; e = el.selectionEnd } catch (_) { /* (a number box has no selection) */ }
  const n = el.value.length
  if (typeof s !== "number" || typeof e !== "number") return [n, n]
  return [Math.min(s, e), Math.max(s, e)]
}

// Put text into el at its cursor (replacing the selection) and tell Vue (an "input" event, so v-model follows).
// Returns the text that went in ("" when nothing fit).
export function insertText(el, text) {
  if (typeof text !== "string" || !text) return ""
  const tag = (el.tagName || "").toUpperCase()
  if (tag === "INPUT") text = text.replace(/[ ]*[\r\n\t]+[ ]*/g, " ").trim()     // (one line)
  const [start, end] = selection(el)
  const max = el.maxLength > 0 ? el.maxLength : Infinity
  const room = max - (el.value.length - (end - start))
  if (room <= 0 || !text) return ""
  text = text.slice(0, room)
  el.value = el.value.slice(0, start) + text + el.value.slice(end)
  try { el.setSelectionRange(start + text.length, start + text.length) } catch (_) { /* (number box) */ }
  const Ev = (typeof window !== "undefined" && window.Event) || null
  if (Ev && el.dispatchEvent) el.dispatchEvent(new Ev("input", { bubbles: true }))
  return text
}

function onKeydown(e) {
  if (!isPaste(e) || !isTextBox(e.target)) return
  const api = window.bngApi
  if (!api || !api.engineLua) return            // (a plain browser: its own paste works)
  e.preventDefault()
  e.stopPropagation()
  const el = e.target
  api.engineLua("getClipboard()", text => insertText(el, typeof text === "string" ? text : ""))
}

export function installPaste() {
  if (typeof document === "undefined") return
  removePaste()                                  // (one listener, whichever copy of the module loaded last)
  window[KEY] = onKeydown
  document.addEventListener("keydown", onKeydown, true)
}

export function removePaste() {
  if (typeof document === "undefined" || !window[KEY]) return
  document.removeEventListener("keydown", window[KEY], true)
  window[KEY] = null
}

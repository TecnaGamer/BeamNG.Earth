// node mod/tests/test_paste.mjs: paste.js, Ctrl+V in the mod's text boxes through the engine's clipboard
import assert from "node:assert/strict"

const listeners = {}
globalThis.window = { Event: class { constructor(type, o) { this.type = type; this.bubbles = o && o.bubbles } } }
globalThis.document = {
  addEventListener: (t, f, cap) => { (listeners[t] ||= []).push([f, cap]) },
  removeEventListener: (t, f) => { listeners[t] = (listeners[t] || []).filter(([g]) => g !== f) },
}
const P = await import("../beamng_earth/ui/ui-vue/mods/beamngEarth/paste.js")

function box({ tag = "INPUT", type = "text", value = "", sel = [value.length, value.length], max = -1, inMod = true,
               disabled = false, readOnly = false } = {}) {
  const el = { tagName: tag, type, value, maxLength: max, disabled, readOnly, events: [],
    selectionStart: sel[0], selectionEnd: sel[1],
    setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b },
    dispatchEvent(ev) { this.events.push(ev.type) },
    closest: sel_ => (inMod ? {} : null) }
  return el
}

// insertText: at the cursor, replacing the selection, one line for an <input>, within maxlength, an input event
let el = box({ value: "Balt more", sel: [4, 4] })
assert.equal(P.insertText(el, "i"), "i")
assert.equal(el.value, "Balti more")
assert.deepEqual(el.events, ["input"])
assert.equal(el.selectionStart, 5)
el = box({ value: "github.com/old", sel: [0, 14] })
P.insertText(el, "github.com/owner/name\n")
assert.equal(el.value, "github.com/owner/name", "the selection replaced, the trailing newline gone")
el = box({ value: "" })
P.insertText(el, "36.5785,\n -118.2923")
assert.equal(el.value, "36.5785, -118.2923", "lines joined in a one-line box")
el = box({ value: "abc", max: 5 })
assert.equal(P.insertText(el, "defgh"), "de", "only what fits the maxlength")
assert.equal(el.value, "abcde")
el = box({ value: "abcde", max: 5 })
assert.equal(P.insertText(el, "x"), "", "a full box takes nothing")
assert.deepEqual(el.events, [])
el = box({ tag: "TEXTAREA", value: "a" })
P.insertText(el, "\nb")
assert.equal(el.value, "a\nb", "a textarea keeps its lines")
el = box({ type: "number", value: "1", sel: [null, null] })
P.insertText(el, "2")
assert.equal(el.value, "12", "a number box: appended (no selection there)")

// isPaste / isTextBox
assert.ok(P.isPaste({ ctrlKey: true, key: "v" }))
assert.ok(P.isPaste({ metaKey: true, key: "V" }))
assert.ok(P.isPaste({ ctrlKey: true, code: "KeyV", key: "м" }), "another keyboard layout: by the key's position")
assert.ok(P.isPaste({ shiftKey: true, key: "Insert" }))
assert.ok(!P.isPaste({ ctrlKey: true, key: "c" }))
assert.ok(!P.isPaste({ ctrlKey: true, altKey: true, key: "v" }), "AltGr+V is a character, not a paste")
assert.ok(P.isTextBox(box()))
assert.ok(P.isTextBox(box({ tag: "TEXTAREA" })))
assert.ok(!P.isTextBox(box({ type: "checkbox" })))
assert.ok(!P.isTextBox(box({ inMod: false })), "the stock UI's boxes are left alone")
assert.ok(!P.isTextBox(box({ disabled: true })) && !P.isTextBox(box({ readOnly: true })))

// the listener: Ctrl+V in a box of the mod asks the engine's clipboard and puts it in; one listener after two installs
P.installPaste()
P.installPaste()
assert.equal((listeners.keydown || []).length, 1)
assert.equal(listeners.keydown[0][1], true, "capturing: the boxes' own @keydown.stop cannot hide it")
let asked = null
window.bngApi = { engineLua: (cmd, cb) => { asked = cmd; cb("Fells Point") } }
el = box({ value: "" })
let prevented = false, stopped = false
listeners.keydown[0][0]({ ctrlKey: true, key: "v", target: el, preventDefault: () => { prevented = true },
                          stopPropagation: () => { stopped = true } })
assert.equal(asked, "getClipboard()")
assert.equal(el.value, "Fells Point")
assert.ok(prevented && stopped)
// ... not in the stock UI's boxes, and not without the game (a browser pastes by itself)
asked = null
listeners.keydown[0][0]({ ctrlKey: true, key: "v", target: box({ inMod: false }), preventDefault() {}, stopPropagation() {} })
assert.equal(asked, null)
delete window.bngApi
el = box()
listeners.keydown[0][0]({ ctrlKey: true, key: "v", target: el, preventDefault() { throw new Error("prevented") },
                          stopPropagation() {} })
assert.equal(el.value, "")
P.removePaste()
assert.equal((listeners.keydown || []).length, 0)
console.log("paste: insert at the cursor, maxlength, one line, keys, boxes, listener: OK")

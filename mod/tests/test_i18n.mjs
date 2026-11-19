import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"
import { createTranslator, localeChain } from "../beamng_earth/ui/ui-vue/mods/beamngEarth/i18nCore.js"

const catalogs = new URL("../beamng_earth/ui/ui-vue/mods/beamngEarth/locales/", import.meta.url)
const read = name => JSON.parse(readFileSync(new URL(name + ".json", catalogs), "utf-8"))
const manifest = read("catalogs")
const en = read("en")
const pl = read("pl-PL")

test("bundled Polish, aliases, Unicode, switching and regional formatting", () => {
  const ui = createTranslator(manifest, en)
  ui.load("pl-PL", pl)
  ui.select("PL_pl")
  assert.equal(ui.t("settings.language.label"), "Język interfejsu")
  assert.equal(ui.number(1.5, 1), "1,5")
  assert.match(ui.date("2026-11-19"), /19.*lis.*2026/)
  ui.select("fr-CA")
  assert.equal(ui.t("settings.language.label"), "Interface language")
  assert.equal(ui.number(1.5, 1), "1.5")
  ui.select("pl")
  assert.equal(ui.t("settings.language.label"), "Język interfejsu")
})

test("regional fallback, invalid patterns, invalid catalogs and deduplicated diagnostics", () => {
  const locales = { en: {}, pl: {}, "pl-PL": {} }
  const source = { "test.greeting": "Hello {name}", "test.help": "Help", "test.close": "Close" }
  const ui = createTranslator(locales, source)
  ui.load("pl", { "test.greeting": "Witaj {name}", "test.help": "Pomoc" })
  ui.load("pl-PL", { "test.help": "Pomoc techniczna", "test.greeting": "Witaj {user}", "test.close": [1] })
  ui.select("pl-PL")
  assert.deepEqual(localeChain("pl_PL", locales), ["pl-PL", "pl", "en"])
  assert.deepEqual(localeChain("en-US", locales), ["en"])
  assert.equal(ui.t("test.help"), "Pomoc techniczna")
  assert.equal(ui.t("test.greeting", { name: "<b>{later}</b>" }), "Witaj <b>{later}</b>")
  assert.equal(ui.t("test.close"), "Close")
  for (let repeat = 0; repeat < 4; ++repeat) assert.equal(ui.t("test.greeting", { name: {} }), "Witaj —")
  assert.equal(ui.t("test.missing"), "—")
  assert.equal(ui.report().issues.filter(issue => issue.includes("invalid parameter")).length, 1)
  assert.equal(ui.report().missing.length, 2)
  ui.load("pl-PL", null)
  assert.equal(ui.t("test.help"), "Pomoc")
})

test("Polish CLDR rules and the language of the fallback message", () => {
  const ui = createTranslator(manifest, en)
  ui.load("pl-PL", pl)
  ui.select("pl-PL")
  for (const [count, wording] of [[1, "1 plik"], [2, "2 pliki"], [5, "5 plików"], [12, "12 plików"],
    [21, "21 plików"], [22, "22 pliki"], [1.5, "1,5 pliku"]]) {
    assert.equal(ui.t("hud.loading.files", { count }), "pobieranie: " + wording)
  }
  ui.load("pl-PL", {})
  assert.equal(ui.t("hud.loading.files", { count: 1 }), "downloading 1 file")
  assert.equal(ui.t("hud.loading.files", { count: 22 }), "downloading 22 files")
})

test("all bundled Polish messages load without fallback", () => {
  const ui = createTranslator(manifest, en)
  ui.load("pl-PL", pl)
  ui.select("pl")
  assert.deepEqual(ui.report().issues, [])
  assert.deepEqual(ui.report().missing, [])
  assert.equal(ui.report().coverage, 1)
})

test("separately evaluated UI modules share a preference and a bounded catalog load", async () => {
  const descriptor = new URL("../beamng_earth/ui/ui-vue/mods/beamngEarth/i18n.js", import.meta.url)
  const raw = readFileSync(descriptor, "utf-8")
  const source = raw.replace('from "vue"', 'from "data:text/javascript,export const ref=x=>({value:x});export const computed=f=>({get value(){return f()}});export const watch=()=>()=>{}"')
    .replace(/import \{ useSettings \}[^\n]+/, 'const useSettings = () => ({values:{}})')
    .replace('"./i18nCore.js"', JSON.stringify(new URL("./i18nCore.js", descriptor).href))
    .replace(/import manifest[^\n]+/, 'const manifest = ' + JSON.stringify(manifest))
    .replace('fetch(ROOT', 'globalThis.__earthFetch(ROOT')
  const symbol = Symbol.for("beamngEarth.localization")
  const originalWindow = globalThis.window
  const requested = []
  const writes = []
  globalThis.__earthFetch = async url => {
    requested.push(url.split("/").at(-1))
    return { ok: true, json: async () => url.endsWith("/en.json") ? en : pl }
  }
  globalThis.window = { bngApi: { engineLua(command, callback) {
    writes.push(command)
    callback(true)
  } } }
  try {
    const first = await import("data:text/javascript," + encodeURIComponent(source))
    const second = await import("data:text/javascript," + encodeURIComponent(source + "\n// separately evaluated"))
    assert.equal(first.preference, second.preference)
    await first.readyLocalization()
    assert.equal(second.t("settings.language.label"), "Interface language")
    await first.setGameLocale("pl_PL")
    assert.equal(second.t("settings.language.label"), "Język interfejsu")
    await second.chooseLanguage("en")
    assert.equal(first.preference.value, "en")
    assert.equal(first.t("settings.language.label"), "Interface language")
    await Promise.all([first.chooseLanguage("pl-PL"), second.chooseLanguage("auto")])
    assert.match(writes[0], /"en"/)
    assert.match(writes[1], /"pl-PL"/)
    assert.match(writes[2], /"auto"/)
    assert.deepEqual(requested, ["en.json", "pl-PL.json"])
    assert.equal(second.preference.value, "auto")
    assert.equal(first.t("settings.language.label"), "Język interfejsu")
  } finally {
    delete globalThis[symbol]
    delete globalThis.__earthFetch
    globalThis.window = originalWindow
  }
})

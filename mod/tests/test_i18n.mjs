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
  ui.select("sv-SE")
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

const gameLocales = {
  en: "en-US", "de-DE": "de_DE", "es-ES": "es_ES", "es-419": "es_419",
  "fr-FR": "fr_FR", "ja-JP": "ja_JP", "ko-KR": "ko_KR", "pl-PL": "pl_PL",
  "pt-BR": "pt_BR", "pt-PT": "pt_PT", "ru-RU": "ru_RU",
  "zh-Hans": "zh_Hans", "zh-Hant": "zh_Hant",
}

test("all bundled game IDs resolve to their catalogs and have matching native metadata", () => {
  assert.deepEqual(Object.keys(manifest).sort(), Object.keys(gameLocales).sort())
  const native = folder => JSON.parse(readFileSync(
    new URL("../beamng_earth/locales/translations/" + folder + "/beamngEarth.translations.json", import.meta.url), "utf8"))
  const nativeKeys = Object.keys(native("en-US")).sort()
  const ui = createTranslator(manifest, en)
  for (const [tag, game] of Object.entries(gameLocales)) {
    assert.equal(manifest[tag].gameLocale, game)
    for (const variant of [game, game.toLowerCase().replaceAll("_", "-")]) {
      assert.deepEqual(localeChain(variant, manifest), tag === "en" ? ["en"] : [tag, "en"], variant)
    }
    assert.deepEqual(Object.keys(native(game)).sort(), nativeKeys)
    const catalog = read(tag)
    assert.ok(ui.load(tag, catalog))
    ui.select(game)
    assert.equal(ui.t("settings.language.label"), catalog["settings.language.label"])
  }
  for (const [tag, expected] of Object.entries({
    "es-MX": "es-419", es: "es-ES", pt: "pt-BR",
    "zh-CN": "zh-Hans", "zh_HK": "zh-Hant", "zh_TW": "zh-Hant", zh: "zh-Hans", "fr_CA": "fr-FR",
  })) assert.deepEqual(localeChain(tag, manifest), [expected, "en"])
  assert.deepEqual(ui.report().issues, [])
})

test("regional defaults never fill missing messages from a sibling catalog", () => {
  const ui = createTranslator(manifest, { "test.key": "English" })
  for (const [sibling, selection] of [
    ["es-ES", "es_MX"], ["pt-BR", "pt_PT"], ["zh-Hans", "zh_Hant"], ["zh-Hans", "zh-HK"],
  ]) {
    ui.load(sibling, { "test.key": "Sibling" })
    ui.select(selection)
    assert.equal(ui.t("test.key"), "English", selection)
  }
})

let runtimeId = 0

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

function runtime(t, { fetchCatalog, bridge, catalogsManifest = manifest } = {}) {
  const descriptor = new URL("../beamng_earth/ui/ui-vue/mods/beamngEarth/i18n.js", import.meta.url)
  const vue = "data:text/javascript,export const ref=x=>({value:x});export const computed=f=>({get value(){return f()}});export const watch=()=>()=>{}"
  const raw = readFileSync(descriptor, "utf-8")
  const source = raw.replace('from "vue"', 'from "' + vue + '"')
    .replace(/import \{ useSettings \}[^\n]+/, 'const useSettings = () => ({values:{}})')
    .replace('"./i18nCore.js"', JSON.stringify(new URL("./i18nCore.js", descriptor).href))
    .replace(/import catalogManifest[^\n]+/, 'const catalogManifest = ' + JSON.stringify(catalogsManifest))
    .replace('fetch(ROOT', 'globalThis.__earthFetch(ROOT')
    .replaceAll("performance.now()", "globalThis.__earthNow()")
    .replace("LOAD_TIMEOUT_MS = 1800", "LOAD_TIMEOUT_MS = 40")
  const symbol = Symbol.for("beamngEarth.localization")
  const originalWindow = globalThis.window
  const requested = []
  const writes = []
  let now = 0
  globalThis.__earthNow = () => now
  globalThis.__earthFetch = (url, options) => {
    const locale = url.split("/").at(-1).slice(0, -5)
    requested.push(locale)
    return fetchCatalog ? fetchCatalog(locale, options) : response(locale === "en" ? en : pl)
  }
  globalThis.window = { bngApi: { engineLua(command, callback) {
    writes.push(command)
    if (bridge) bridge(command, callback)
    else callback(true)
  } } }
  t.after(() => {
    delete globalThis[symbol]
    delete globalThis.__earthFetch
    delete globalThis.__earthNow
    globalThis.window = originalWindow
  })
  return {
    copy: () => import("data:text/javascript," + encodeURIComponent(source + "\n// runtime " + ++runtimeId)),
    advance: () => { now += 2501 }, requested, writes,
  }
}

const response = entries => ({ ok: true, json: async () => entries })

test("the in-game selector lists all 13 catalogs and can override automatic language detection", async t => {
  const host = runtime(t, { fetchCatalog: locale => response(read(locale)) })
  const ui = await host.copy()
  assert.deepEqual(ui.languageOptions.value.map(option => option.value).sort(), Object.keys(gameLocales).sort())
  for (const [tag, game] of Object.entries(gameLocales)) {
    await ui.setGameLocale(game)
    assert.equal(ui.localizationReport().active, tag, game)
    assert.equal(ui.t("settings.language.label"), read(tag)["settings.language.label"])
  }
  await ui.chooseLanguage("pt-PT")
  await ui.setGameLocale("es_419")
  assert.equal(ui.localizationReport().active, "pt-PT")
  await ui.chooseLanguage("auto")
  assert.equal(ui.localizationReport().active, "es-419")
})

test("separately evaluated UI modules share the preference and one request per catalog", async t => {
  const host = runtime(t)
  const first = await host.copy()
  const second = await host.copy()
  assert.equal(first.preference, second.preference)
  await Promise.all([first.readyLocalization(), second.readyLocalization()])
  assert.equal(second.t("settings.language.label"), "Interface language")
  await Promise.all([first.setGameLocale("pl_PL"), second.setGameLocale("pl_PL")])
  assert.equal(second.t("settings.language.label"), "Język interfejsu")
  await second.chooseLanguage("en")
  assert.equal(first.preference.value, "en")
  assert.equal(first.t("settings.language.label"), "Interface language")
  await Promise.all([first.chooseLanguage("pl-PL"), second.chooseLanguage("auto")])
  assert.match(host.writes[0], /"en"/)
  assert.match(host.writes[1], /"pl-PL"/)
  assert.match(host.writes[2], /"auto"/)
  assert.deepEqual(host.requested, ["en", "pl-PL"])
  assert.equal(second.preference.value, "auto")
  assert.equal(first.t("settings.language.label"), "Język interfejsu")
})

for (const [label, failure, diagnostic] of [
  ["HTTP error", () => ({ ok: false, status: 503 }), /HTTP 503/],
  ["malformed JSON", () => ({ ok: true, json: async () => { throw new SyntaxError("invalid JSON") } }), /invalid JSON/],
  ["invalid root", () => response([]), /invalid catalog/],
]) {
  test(`${label} falls back, deduplicates retries and recovers`, async t => {
    let recover = false
    const host = runtime(t, { fetchCatalog: locale => locale === "en" ? response(en) : recover ? response(pl) : failure() })
    const ui = await host.copy()
    await Promise.all([ui.setGameLocale("pl_PL"), ui.setGameLocale("pl_PL")])
    assert.equal(ui.t("settings.language.label"), "Interface language")
    assert.match(ui.localizationReport().issues.join("\n"), diagnostic)
    recover = true
    await ui.setGameLocale("pl_PL")
    assert.deepEqual(host.requested, ["en", "pl-PL"])
    host.advance()
    await Promise.all([ui.setGameLocale("pl_PL"), ui.setGameLocale("pl_PL")])
    assert.deepEqual(host.requested, ["en", "pl-PL", "pl-PL"])
    assert.equal(ui.t("settings.language.label"), "Język interfejsu")
  })
}

test("a hung request aborts at the deadline and a late body cannot replace a healthy retry", async t => {
  const body = deferred()
  let signal
  let recover = false
  const host = runtime(t, { fetchCatalog: (locale, options) => {
    if (locale === "en") return response(en)
    if (recover) return response(pl)
    signal = options.signal
    return { ok: true, json: () => body.promise }
  } })
  const ui = await host.copy()
  await ui.setGameLocale("pl_PL")
  assert.equal(signal.aborted, true)
  assert.equal(ui.t("settings.language.label"), "Interface language")
  assert.match(ui.localizationReport().issues.join("\n"), /timed out/)
  recover = true
  host.advance()
  await ui.setGameLocale("pl_PL")
  body.resolve({ "settings.language.label": "Stara etykieta" })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(ui.t("settings.language.label"), "Język interfejsu")
})

test("English failure leaves a safe placeholder and recovers both English and the requested language", async t => {
  let recover = false
  const host = runtime(t, { fetchCatalog: locale => locale === "en" ? response(recover ? en : {}) : response(pl) })
  const ui = await host.copy()
  await ui.setGameLocale("pl_PL")
  assert.equal(ui.t("settings.language.label"), "—")
  assert.deepEqual(host.requested, ["en"])
  assert.match(ui.localizationReport().issues.join("\n"), /invalid catalog/)
  recover = true
  await ui.setGameLocale("pl_PL")
  assert.deepEqual(host.requested, ["en"])
  host.advance()
  await ui.setGameLocale("pl_PL")
  assert.deepEqual(host.requested, ["en", "en", "pl-PL"])
  assert.equal(ui.t("settings.language.label"), "Język interfejsu")
  await ui.chooseLanguage("en")
  assert.equal(ui.t("settings.language.label"), "Interface language")
})

test("an English selection overtakes a pending Polish fetch without reordering persistence", async t => {
  const polishRequested = deferred()
  const polishResponse = deferred()
  const host = runtime(t, { fetchCatalog: locale => {
    if (locale === "en") return response(en)
    polishRequested.resolve()
    return polishResponse.promise
  } })
  const ui = await host.copy()
  const polish = ui.chooseLanguage("pl-PL")
  await polishRequested.promise
  await ui.chooseLanguage("en")
  assert.equal(host.writes.length, 2)
  assert.equal(ui.preference.value, "en")
  polishResponse.resolve(response(pl))
  await polish
  assert.equal(ui.t("settings.language.label"), "Interface language")
  assert.equal(ui.localizationReport().active, "en")
})

test("remount refreshes catalog contents shared by existing components and ignores a stale preference reply", async t => {
  const replies = []
  let current = en
  const host = runtime(t, {
    fetchCatalog: locale => response(locale === "en" ? current : pl),
    bridge: (command, callback) => command.includes("uiLanguagePreference") ? replies.push(callback) : callback(true),
  })
  const first = await host.copy()
  const second = await host.copy()
  const stop = first.startLocalization()
  await first.chooseLanguage("pl-PL")
  replies[0]("en")
  assert.equal(first.preference.value, "pl-PL")
  stop()
  current = { ...en, "settings.language.label": "Updated catalog" }
  const stopAgain = second.startLocalization()
  await second.chooseLanguage("en")
  assert.equal(first.t("settings.language.label"), "Updated catalog")
  assert.equal(host.requested.filter(locale => locale === "en").length, 2)
  stopAgain()
})

import { computed, ref, watch } from "vue"
import { useSettings } from "@/services/settings"
import { createTranslator, localeChain, localeRegistry } from "./i18nCore.js"
import catalogManifest from "./locales/catalogs.json"

const manifest = localeRegistry(catalogManifest)

// BeamNG can evaluate a dependency separately for its mod entry point and for a
// mounted SFC. Both need the same refs and catalogs for live language changes.
const SHARED = Symbol.for("beamngEarth.localization")
const shared = globalThis[SHARED] ||= {
  translator: createTranslator(manifest),
  registry: ref(manifest), catalogs: new Map(), revision: ref(0),
  gameLocale: ref(""), preference: ref("auto"),
  sequence: 0, writes: 0, persistence: Promise.resolve(), baseReady: false, mountedBefore: false,
}
const { registry, catalogs, revision, gameLocale } = shared
export const preference = shared.preference
export const languageOptions = computed(() => Object.entries(registry.value).map(([value, meta]) => ({ value, label: meta.name })))
export const direction = computed(() => {
  revision.value
  return registry.value[shared.translator.active()]?.direction || "ltr"
})

const ROOT = "/ui/ui-vue/mods/beamngEarth/locales/"
const LOAD_TIMEOUT_MS = 1800
const RETRY_DELAY_MS = 2500

function load(locale) {
  const previous = catalogs.get(locale)
  if (previous?.promise) return previous.promise
  if (previous?.loaded) return true
  if (previous && performance.now() < previous.retryAt) return false

  const controller = new AbortController()
  const entry = { controller, promise: null, loaded: false, retryAt: 0 }
  catalogs.set(locale, entry)
  const aborted = new Promise((_, reject) => {
    controller.signal.addEventListener("abort", () => reject(new Error("request timed out or was cancelled")), { once: true })
  })
  const timeout = setTimeout(() => controller.abort(), LOAD_TIMEOUT_MS)
  const read = async () => {
    const response = await fetch(ROOT + locale + ".json", { signal: controller.signal, cache: "no-store" })
    if (!response.ok) throw new Error("HTTP " + response.status)
    try { return await response.json() }
    catch { throw new Error("invalid JSON") }
  }
  entry.promise = Promise.race([aborted, read()]).then(entries => {
    if (catalogs.get(locale) !== entry) return false
    if (!shared.translator.load(locale, entries)) throw new Error("invalid catalog")
    entry.loaded = true
    return true
  }).catch(error => {
    if (catalogs.get(locale) === entry) {
      entry.retryAt = performance.now() + RETRY_DELAY_MS
      shared.translator.issue(`Could not read catalog: ${locale} (${error?.message || "request failed"})`)
    }
    return false
  }).finally(() => {
    clearTimeout(timeout)
    entry.promise = entry.controller = null
  })
  return entry.promise
}

async function applyLocale() {
  const request = ++shared.sequence
  const tag = preference.value === "auto" ? gameLocale.value : preference.value
  if (!await readyLocalization() || request !== shared.sequence) return
  const chain = localeChain(tag, registry.value)
  await Promise.all(chain.map(load))
  if (request !== shared.sequence) return
  shared.translator.select(tag)
  revision.value++
}

export function setGameLocale(tag) {
  if (typeof tag !== "string") return
  gameLocale.value = tag
  if (preference.value === "auto") return applyLocale()
}

function receivePreference(value) {
  preference.value = typeof value === "string" && Object.hasOwn(registry.value, value) ? value : "auto"
  return applyLocale()
}

export function chooseLanguage(value) {
  if (typeof value !== "string") return
  if (value !== "auto" && !Object.hasOwn(registry.value, value)) return
  // Route only registered locale identifiers to Lua and serialize successive writes.
  const save = () => new Promise(resolve => {
    const api = window.bngApi
    if (!api?.engineLua) return resolve(false)
    const timeout = setTimeout(() => resolve(false), 3000)
    const finish = result => {
      clearTimeout(timeout)
      resolve(result === true)
    }
    try { api.engineLua(`beamngEarth_settings and beamngEarth_settings.uiSetLanguage("${value}")`, finish) }
    catch { finish(false) }
  })
  shared.writes++
  let applied
  shared.persistence = shared.persistence.then(save).then(ok => {
    if (ok) applied = receivePreference(value)
    else shared.translator.issue("Could not save the language preference")
  })
  // A slow catalog read must not block the next player's choice from reaching Lua.
  return shared.persistence.then(() => applied)
}

export async function readyLocalization() {
  if (shared.baseReady) return true
  const translator = shared.translator
  if (!await load("en") || translator !== shared.translator) return false
  if (!shared.baseReady) {
    shared.baseReady = true
    revision.value++
  }
  return true
}

export function t(key, params) {
  revision.value
  return shared.baseReady ? shared.translator.t(key, params) : "—"
}

// Defer translation of notices kept in reactive state across a language change.
export function notice(key, params) {
  return () => t(key, params)
}

export function messageText(value) {
  return typeof value === "function" ? value() : value
}

export function number(value, digits) {
  revision.value
  return shared.translator.number(value, digits)
}

export function date(value) {
  revision.value
  return shared.translator.date(value)
}

// Opt-in in the game's CEF console. No diagnostics polling or developer UI in normal use.
export function localizationReport() {
  const i18n = shared.translator
  i18n.enableDiagnostics()
  return { preference: preference.value, detected: gameLocale.value, ...i18n.report() }
}

export function watchLocale(callback) {
  return watch(revision, callback)
}

export function startLocalization() {
  if (shared.mountedBefore) {
    // The mod on disk may now be a different release or the server's mounted copy.
    const oldCatalogs = [...catalogs.values()]
    catalogs.clear()
    for (const entry of oldCatalogs) entry.controller?.abort()
    shared.translator = createTranslator(manifest)
    registry.value = manifest
    shared.baseReady = false
    shared.sequence++
    revision.value++
  }
  shared.mountedBefore = true
  let active = true
  void receivePreference(preference.value)
  const settings = useSettings()
  const stopWatch = watch(() => settings.values.uiLanguage, setGameLocale, { immediate: true })
  const writesAtRequest = shared.writes
  try {
    window.bngApi?.engineLua?.("beamngEarth_settings and beamngEarth_settings.uiLanguagePreference()", saved => {
      if (active && typeof saved === "string" && shared.writes === writesAtRequest) receivePreference(saved)
    })
  } catch { shared.translator.issue("Could not read the language preference") }
  window.beamngEarthLocalization = localizationReport
  return () => {
    if (!active) return
    active = false
    shared.sequence++
    stopWatch()
    if (window.beamngEarthLocalization === localizationReport) delete window.beamngEarthLocalization
  }
}

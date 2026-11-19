import { computed, ref, watch } from "vue"
import { useSettings } from "@/services/settings"
import { createTranslator, localeChain } from "./i18nCore.js"
import manifest from "./locales/catalogs.json"

// BeamNG can evaluate a dependency separately for its mod entry point and for a
// mounted SFC. Both need the same refs and catalogs for live language changes.
const SHARED = Symbol.for("beamngEarth.localization")
const shared = globalThis[SHARED] ||= {
  translator: createTranslator(manifest, {}),
  pending: new Map(), revision: ref(0), gameLocale: ref(""), preference: ref("auto"),
  sequence: 0, persistence: Promise.resolve(), baseReady: false,
}
const { translator: i18n, pending, revision, gameLocale } = shared
export const preference = shared.preference
export const languageOptions = Object.entries(manifest).map(([value, meta]) => ({ value, label: meta.name }))
export const direction = computed(() => {
  revision.value
  return manifest[i18n.active()]?.direction || "ltr"
})

const ROOT = "/ui/ui-vue/mods/beamngEarth/locales/"

async function load(locale) {
  if (!pending.has(locale)) {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 1800)
    pending.set(locale, fetch(ROOT + locale + ".json", { signal: controller.signal }).then(response => {
      if (!response.ok) throw new Error(String(response.status))
      return response.json()
    }).then(entries => i18n.load(locale, entries))
      .catch(() => i18n.issue("Could not read catalog: " + locale))
      .finally(() => clearTimeout(timeout)))
  }
  await pending.get(locale)
}

async function applyLocale() {
  const request = ++shared.sequence
  const tag = preference.value === "auto" ? gameLocale.value : preference.value
  await readyLocalization()
  const chain = localeChain(tag, manifest)
  await Promise.all(chain.map(load))
  if (request !== shared.sequence) return
  i18n.select(tag)
  revision.value++
}

export function setGameLocale(tag) {
  if (tag === gameLocale.value || typeof tag !== "string") return
  gameLocale.value = tag
  if (preference.value === "auto") return applyLocale()
}

function receivePreference(value) {
  const next = typeof value === "string" && Object.hasOwn(manifest, value) ? value : "auto"
  if (next === preference.value) return
  preference.value = next
  return applyLocale()
}

export function chooseLanguage(value) {
  if (value !== "auto" && !Object.hasOwn(manifest, value)) return
  // Route only registered locale identifiers to Lua and serialize successive writes.
  const save = () => new Promise(resolve => {
    const api = window.bngApi
    if (!api?.engineLua) return resolve(false)
    const timeout = setTimeout(() => resolve(false), 3000)
    api.engineLua(`beamngEarth_settings and beamngEarth_settings.uiSetLanguage("${value}")`, result => {
      clearTimeout(timeout)
      resolve(result === true)
    })
  })
  shared.persistence = shared.persistence.then(save).then(async ok => {
    if (ok) await receivePreference(value)
    else i18n.issue("Could not save the language preference")
  })
  return shared.persistence
}

export async function readyLocalization() {
  await load("en")
  if (!shared.baseReady) {
    shared.baseReady = true
    revision.value++
  }
}

export function t(key, params) {
  revision.value
  return shared.baseReady ? i18n.t(key, params) : "—"
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
  return i18n.number(value, digits)
}

export function date(value) {
  revision.value
  return i18n.date(value)
}

// Opt-in in the game's CEF console. No diagnostics polling or developer UI in normal use.
export function localizationReport() {
  i18n.enableDiagnostics()
  return { preference: preference.value, detected: gameLocale.value, ...i18n.report() }
}

export function watchLocale(callback) {
  return watch(revision, callback)
}

export function startLocalization() {
  let active = true
  void readyLocalization()
  const settings = useSettings()
  const stopWatch = watch(() => settings.values.uiLanguage, setGameLocale, { immediate: true })
  const prefAtRequest = preference.value
  window.bngApi?.engineLua("beamngEarth_settings and beamngEarth_settings.uiLanguagePreference()", saved => {
    if (active && preference.value === prefAtRequest) receivePreference(saved)
  })
  window.beamngEarthLocalization = localizationReport
  return () => {
    active = false
    stopWatch()
    if (window.beamngEarthLocalization === localizationReport) delete window.beamngEarthLocalization
  }
}

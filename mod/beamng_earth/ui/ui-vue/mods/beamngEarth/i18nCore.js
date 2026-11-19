const owns = (object, key) => Object.prototype.hasOwnProperty.call(object, key)
const placeholder = /\{([a-zA-Z][a-zA-Z0-9_]*)\}/g
const categories = ["zero", "one", "two", "few", "many", "other"]

export function normalizeLocale(tag) {
  if (typeof tag !== "string" || !/^[a-z]{2,3}(?:[-_][a-z0-9]{2,8})*$/i.test(tag.trim())) return ""
  return tag.trim().replace(/_/g, "-").toLowerCase()
}

export function localeChain(tag, manifest) {
  const index = new Map()
  for (const [locale, meta] of Object.entries(manifest)) index.set(normalizeLocale(locale), locale)
  for (const [locale, meta] of Object.entries(manifest)) {
    for (const alias of meta.aliases || []) {
      const normalized = normalizeLocale(alias)
      if (normalized && !index.has(normalized)) index.set(normalized, locale)
    }
  }

  const chain = new Set()
  const parts = normalizeLocale(tag).split("-")
  while (parts.length) {
    const locale = index.get(parts.join("-"))
    if (locale) chain.add(locale)
    parts.pop()
  }
  chain.add("en")
  return [...chain]
}

function parameters(text) {
  return [...new Set(Array.from(text.matchAll(placeholder), match => match[1]))].sort().join(",")
}

function validText(text) {
  return typeof text === "string" && text.trim() && !/[{}]/.test(text.replace(placeholder, ""))
}

export function validMessage(value, source = value) {
  if (typeof source === "string") return validText(source) && validText(value) && parameters(value) === parameters(source)
  if (!value || typeof value !== "object" || Array.isArray(value) || !validText(value.other) || !validText(source?.other)) return false
  const expected = parameters(source.other)
  return expected.split(",").includes("count") && Object.entries(value).every(([form, text]) =>
    categories.includes(form) && validText(text) && parameters(text) === expected)
}

export function createTranslator(manifest, english) {
  const messages = new Map()
  let base = {}
  const issues = new Set()
  const fallbackKeys = new Set()
  const numbers = new Map()
  const plurals = new Map()
  const dates = new Map()
  let chain = ["en"]
  let debug = false

  function issue(message) {
    if (issues.has(message)) return
    issues.add(message)
    if (debug) console.warn("BeamNG.Earth i18n", message)
  }

  function load(locale, entries) {
    const valid = Object.create(null)
    if (!entries || typeof entries !== "object" || Array.isArray(entries)) {
      issue("Invalid catalog: " + locale)
    } else {
      for (const [key, message] of Object.entries(entries)) {
        if (locale !== "en" && (!owns(base, key) || !validMessage(message, base[key]))) {
          issue("Invalid or unknown message: " + locale + "/" + key)
        } else if (validMessage(message)) {
          valid[key] = message
        } else {
          issue("Invalid message: " + locale + "/" + key)
        }
      }
    }
    if (locale === "en") base = valid
    messages.set(locale, valid)
  }

  function select(tag) {
    const next = localeChain(tag, manifest)
    if (next.join() !== chain.join()) fallbackKeys.clear()
    chain = next
  }

  function number(value, digits, locale = chain[0]) {
    if (typeof value !== "number" || !Number.isFinite(value)) return ""
    const key = locale + "/" + (digits ?? "")
    if (!numbers.has(key)) {
      const options = Number.isInteger(digits) && digits >= 0 && digits <= 20
        ? { minimumFractionDigits: digits, maximumFractionDigits: digits }
        : undefined
      try { numbers.set(key, new Intl.NumberFormat(locale, options)) }
      catch { numbers.set(key, null) }
    }
    return numbers.get(key)?.format(value) ?? String(value)
  }

  function date(value) {
    const timestamp = new Date(value)
    if (!Number.isFinite(timestamp.getTime())) return ""
    const locale = chain[0]
    if (!dates.has(locale)) {
      try { dates.set(locale, new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" })) }
      catch { dates.set(locale, null) }
    }
    return dates.get(locale)?.format(timestamp) ?? timestamp.toISOString().slice(0, 10)
  }

  function plural(locale, count) {
    if (!plurals.has(locale)) {
      try { plurals.set(locale, new Intl.PluralRules(locale)) }
      catch { plurals.set(locale, null) }
    }
    return plurals.get(locale)?.select(count) ?? (count === 1 ? "one" : "other")
  }

  function t(key, params) {
    for (const locale of chain) {
      const message = messages.get(locale)?.[key]
      if (message === undefined) continue
      if (locale !== chain[0] && debug) fallbackKeys.add(key)
      let template = message
      if (typeof message !== "string") {
        const count = params?.count
        if (typeof count !== "number" || !Number.isFinite(count)) issue("Invalid count: " + key)
        const form = typeof count === "number" && Number.isFinite(count) ? plural(locale, count) : "other"
        template = owns(message, form) ? message[form] : message.other
      }
      if (!template.includes("{")) return template
      return template.replace(placeholder, (_, name) => {
        const value = params && owns(params, name) ? params[name] : undefined
        if (typeof value === "string") return value
        if (typeof value === "number" && Number.isFinite(value)) return number(value, undefined, locale)
        issue("Missing or invalid parameter: " + key + "/" + name)
        return "—"
      })
    }
    issue("Missing English message: " + key)
    return "—"
  }

  function report() {
    const keys = Object.keys(messages.get("en") || {})
    const active = messages.get(chain[0]) || {}
    const missing = keys.filter(key => !owns(active, key))
    return { active: chain[0], coverage: keys.length ? (keys.length - missing.length) / keys.length : 0,
      missing, fallbacks: [...fallbackKeys].sort(), issues: [...issues].sort() }
  }

  load("en", english)
  return { t, load, select, number, date, report, issue, active: () => chain[0], enableDiagnostics() { debug = true } }
}

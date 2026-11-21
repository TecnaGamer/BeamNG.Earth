-- Shared catalogs with the Vue UI. The preference is client-local in the user folder.
local M = { revision = 0 }
local root = '/ui/ui-vue/mods/beamngEarth/locales/'
local settingsPath = '/settings/beamngEarth-language.json'
local catalogs, chain, manifest, choice, game = {}, {'en'}, nil, 'auto', ''
local retryAt, validated = {}, {}
local diagnostics, issues = false, {}
local initialized = false

local function issue(message)
  if issues[message] then return end
  issues[message] = true
  if diagnostics and log then log('W', 'beamngEarth.i18n', message) end
end

local function read(path)
  if not jsonReadFile then return nil end
  local ok, result = pcall(jsonReadFile, path)
  return ok and type(result) == 'table' and result or nil
end

local function normalize(tag)
  if type(tag) ~= 'string' then return '' end
  local value = tag:match('^%s*(.-)%s*$'):lower():gsub('_', '-')
  local language, rest = value:match('^([a-z][a-z][a-z]?)(.*)$')
  if not language then return '' end
  while rest ~= '' do
    local part, remaining = rest:match('^%-([a-z0-9]+)(.*)$')
    if not part or #part < 2 or #part > 8 then return '' end
    rest = remaining
  end
  return value
end

local function registry()
  local manifest = {en = {}}
  local seen = {en = true}
  for tag, meta in pairs(read(root .. 'catalogs.json') or {}) do
    local key = normalize(tag)
    if key ~= '' and key == tag:lower() and (not seen[key] or tag == 'en')
      and type(meta) == 'table' and type(meta.name) == 'string' and meta.name:find('%S') then
      manifest[tag], seen[key] = meta, true
    end
  end
  return manifest
end

local function refresh()
  local now = os.time()
  local tags, seen = {}, {}
  for tag in pairs(manifest) do tags[normalize(tag)] = tag end
  for tag, meta in pairs(manifest) do
    if type(meta) == 'table' and type(meta.aliases) == 'table' then
      for _, alias in ipairs(meta.aliases) do
        local key = normalize(alias)
        if key ~= '' then tags[key] = tags[key] or tag end
      end
    end
  end
  local function add(tag)
    if not tag or seen[tag] then return end
    seen[tag] = true
    chain[#chain + 1] = tag
    if not catalogs[tag] and now >= (retryAt[tag] or 0) then
      local catalog = read(root .. tag .. '.json')
      if catalog and (tag ~= 'en' or next(catalog)) then
        catalogs[tag], retryAt[tag] = catalog, nil
        if tag == 'en' then validated = {} end
      else
        retryAt[tag] = now + 3
        issue('Could not read catalog: ' .. tag)
      end
    end
  end
  local tag = normalize(choice == 'auto' and game or choice)
  chain = {}
  while tag ~= '' do
    local match = tags[tag]
    if match and (#chain == 0 or normalize(match) == tag) then add(match) end
    tag = tag:match('^(.*)%-[^%-]+$') or ''
  end
  add('en')
  M.revision = M.revision + 1
end

local function gameLanguage()
  if Lua and Lua.getSelectedLanguage then return Lua:getSelectedLanguage() or '' end
  local values = settings and settings.getValue and settings.getValue('uiLanguage')
  if type(values) == 'string' and values ~= '' then return values end
  return ''
end

local function init()
  if initialized then return end
  initialized = true
  manifest = registry()
  local data = read(settingsPath)
  choice = data and manifest[data.language] and data.language or 'auto'
  game = gameLanguage()
  refresh()
end

function M.language()
  init()
  return chain[1]
end

function M.preference()
  init()
  return choice
end

function M.setPreference(tag)
  init()
  if type(tag) ~= 'string' or (tag ~= 'auto' and not manifest[tag]) or not jsonWriteFile then return false end
  if tag == choice then
    refresh()
    return true
  end
  local ok, saved = pcall(jsonWriteFile, settingsPath, {language = tag}, true, nil, true)
  if not ok or not saved then return false end
  choice = tag
  refresh()
  return true
end

function M.gameLanguageChanged()
  if not initialized then return end
  local nextGame = gameLanguage()
  local changed = nextGame ~= game
  game = nextGame
  if (changed and choice == 'auto') or next(retryAt) then refresh() end
end

local function signature(value)
  if type(value) ~= 'string' or not value:find('%S') then return nil end
  local keys, names = {}, {}
  local rest = value:gsub('{([a-zA-Z][a-zA-Z0-9_]*)}', function(name)
    if not keys[name] then names[#names + 1], keys[name] = name, true end
    return ''
  end)
  if rest:find('[{}]') then return nil end
  table.sort(names)
  return table.concat(names, ',')
end

-- Cardinal rules for the shipped locales. Counts passed by the HUD are numbers,
-- so the million category applies only to exact, nonzero integer multiples.
local function plural(tag, count)
  if type(count) ~= 'number' or count ~= count or math.abs(count) == math.huge then return 'other' end
  tag = normalize(tag)
  local language = tag:match('^[a-z]+')
  if language == 'ja' or language == 'ko' or language == 'zh' then return 'other' end
  local n = math.abs(count)
  local integer = n == math.floor(n)
  if language == 'ru' and integer then
    local digit, lastTwo = n % 10, n % 100
    if digit == 1 and lastTwo ~= 11 then return 'one' end
    return digit >= 2 and digit <= 4 and (lastTwo < 12 or lastTwo > 14) and 'few' or 'many'
  end
  if n == 1 or (n < 2 and (language == 'fr' or tag == 'pt-br' or tag == 'pt')) then return 'one' end
  if language == 'pl' and integer then
    local digit, lastTwo = n % 10, n % 100
    return digit >= 2 and digit <= 4 and (lastTwo < 12 or lastTwo > 14) and 'few' or 'many'
  end
  if n > 0 and n % 1000000 == 0 and (language == 'es' or language == 'fr' or language == 'pt') then return 'many' end
  return 'other'
end

local function messageSignature(value)
  if type(value) ~= 'table' then return signature(value) end
  local expected = signature(value.other)
  if not expected or not (',' .. expected .. ','):find(',count,', 1, true) then return nil end
  for form, text in pairs(value) do
    if form ~= 'one' and form ~= 'few' and form ~= 'many' and form ~= 'other' and form ~= 'zero' and form ~= 'two' then return nil end
    if signature(text) ~= expected then return nil end
  end
  return expected
end

function M.t(key, params, emergency)
  if type(key) ~= 'string' then
    issue('Invalid translation key')
    return emergency or '—'
  end
  init()
  local english = catalogs.en and catalogs.en[key]
  for _, tag in ipairs(chain) do
    local catalog = catalogs[tag]
    local template = catalog and catalog[key]
    if template ~= nil then
      local checkKey = tag .. '/' .. key
      local valid = validated[checkKey]
      if valid == nil then
        local actual = messageSignature(template)
        valid = type(template) == type(english) and actual ~= nil and actual == messageSignature(english)
        validated[checkKey] = valid
        if not valid then issue('Invalid message: ' .. checkKey) end
      end
      if valid then
        if type(template) == 'table' then
          local count = type(params) == 'table' and params.count
          if type(count) ~= 'number' or count ~= count or math.abs(count) == math.huge then issue('Invalid count: ' .. key) end
          local form = plural(tag, count)
          template = template[form] or template.other
        end
        if not template:find('{', 1, true) then return template end
        return (template:gsub('{([a-zA-Z][a-zA-Z0-9_]*)}', function(name)
          local value = type(params) == 'table' and params[name] or nil
          if type(value) == 'string' or (type(value) == 'number' and value == value and math.abs(value) < math.huge) then
            return tostring(value)
          end
          issue('Missing or invalid parameter: ' .. key .. '/' .. name)
          return '—'
        end))
      end
    end
  end
  issue('Missing English message: ' .. key)
  return emergency or '—'
end

function M.report()
  init()
  diagnostics = true
  local result = {active = chain[1], detected = game, preference = choice, issues = {}}
  for message in pairs(issues) do result.issues[#result.issues + 1] = message end
  table.sort(result.issues)
  return result
end

return M

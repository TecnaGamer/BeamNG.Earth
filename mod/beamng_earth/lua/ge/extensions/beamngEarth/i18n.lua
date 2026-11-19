-- Shared catalogs with the Vue UI. The preference is client-local in the user folder.
local M = { revision = 0 }
local root = '/ui/ui-vue/mods/beamngEarth/locales/'
local settingsPath = '/settings/beamngEarth-language.json'
local catalogs, chain, manifest, choice, game = {}, {'en'}, nil, 'auto', ''
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
  return type(tag) == 'string' and tag:match('^%s*([a-zA-Z][a-zA-Z][a-zA-Z0-9_%-]*)%s*$')
    and tag:match('^%s*(.-)%s*$'):lower():gsub('_', '-') or ''
end

local function refresh()
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
    if not catalogs[tag] then
      local catalog = read(root .. tag .. '.json')
      if not catalog then issue('Could not read catalog: ' .. tag) end
      catalogs[tag] = catalog or {}
    end
  end
  local tag = normalize(choice == 'auto' and game or choice)
  chain = {}
  while tag ~= '' do
    add(tags[tag])
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
  manifest = read(root .. 'catalogs.json') or {en = {}}
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
  if tag == choice then return true end
  local ok, saved = pcall(jsonWriteFile, settingsPath, {language = tag}, true, nil, true)
  if not ok or not saved then return false end
  choice = tag
  refresh()
  return true
end

function M.gameLanguageChanged()
  if not initialized then return end
  local nextGame = gameLanguage()
  if nextGame == game then return end
  game = nextGame
  if choice == 'auto' then refresh() end
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

local validated = {}
-- Cardinal rules for the languages with plural messages on the Lua side.
local function plural(tag, count)
  if type(count) ~= 'number' or count ~= count or math.abs(count) == math.huge then return 'other' end
  local n = math.abs(count)
  if n == 1 then return 'one' end
  if n == math.floor(n) and (normalize(tag):match('^pl%-') or normalize(tag) == 'pl') then
    local digit, lastTwo = n % 10, n % 100
    return digit >= 2 and digit <= 4 and (lastTwo < 12 or lastTwo > 14) and 'few' or 'many'
  end
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
  local english = catalogs.en[key]
  for _, tag in ipairs(chain) do
    local template = catalogs[tag][key]
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

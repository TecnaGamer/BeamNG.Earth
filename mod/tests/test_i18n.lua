package.path = '../beamng_earth/lua/ge/extensions/?.lua;' .. package.path
local game, saved, writes = 'pl_PL', nil, 0
Lua = { getSelectedLanguage = function() return game end }
local messages = {
  ['catalogs.json'] = {en = {}, pl = {}, ['pl-PL'] = {aliases = {'pl'}}},
  ['en.json'] = {greeting = 'Hello {name}', base = 'English', files = {one = '{count} file', other = '{count} files'}},
  ['pl.json'] = {base = 'Po polsku'},
  ['pl-PL.json'] = {greeting = 'Cześć {name}', base = 'Niepoprawne {value}',
    files = {one = '{count} plik', few = '{count} pliki', many = '{count} plików', other = '{count} pliku'}},
}
function jsonReadFile(path)
  if path == '/settings/beamngEarth-language.json' then return saved end
  return messages[path:match('[^/]+$')]
end
function jsonWriteFile(path, value, _, _, atomic)
  assert(path == '/settings/beamngEarth-language.json' and atomic)
  writes, saved = writes + 1, value
  return true
end
local i18n = require('beamngEarth/i18n')
assert(i18n.preference() == 'auto')
assert(i18n.language() == 'pl-PL')
assert(i18n.t('greeting', { name = 'Łódź' }) == 'Cześć Łódź')
assert(i18n.t('base') == 'Po polsku')
for count, expected in pairs({[1] = 'plik', [2] = 'pliki', [5] = 'plików', [21] = 'plików', [22] = 'pliki'}) do
  assert(i18n.t('files', { count = count }) == count .. ' ' .. expected)
end
assert(i18n.t('greeting') == 'Cześć —')
assert(i18n.setPreference('en') and writes == 1)
assert(not i18n.setPreference('../no'))
assert(i18n.t('base') == 'English')
local reloaded = dofile('../beamng_earth/lua/ge/extensions/beamngEarth/i18n.lua')
assert(reloaded.preference() == 'en')
assert(reloaded.setPreference('auto'))
game = 'fr_CA'
reloaded.gameLanguageChanged()
assert(reloaded.language() == 'en')
game = 'pl_PL'
messages['pl-PL.json'] = false
local damaged = dofile('../beamng_earth/lua/ge/extensions/beamngEarth/i18n.lua')
assert(damaged.t('greeting', { name = 'Łódź' }) == 'Hello Łódź')
assert(damaged.t('missing') == '—')
assert(#damaged.report().issues == 2)
print('test_i18n: preference, regional fallback, plural rules and invalid catalogs passed')

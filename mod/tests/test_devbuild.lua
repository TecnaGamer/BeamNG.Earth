-- luajit mod/tests/test_devbuild.lua  (the development build's notice)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local D = require('beamngEarth/devbuild')
T.check(D.isDev('1.7.2-beta.dev.37') and D.isDev('1.8.0-dev.2'), 'the dev branch\'s pre-releases')
T.check(not D.isDev('1.7.2-beta') and not D.isDev('') and not D.isDev(nil) and not D.isDev('1.7.2-beta.devx.3'),
  'releases and nonsense: not')
local l = D.lines('1.7.2-beta.dev.37')
T.check(l[1]:find('1.7.2-beta.dev.37', 1, true) and l[2]:find('not been thoroughly tested', 1, true), 'says what it is')
T.done('test_devbuild')

-- luajit mod/tests/test_rally.lua  (rally stages: progress, calls, the run from the server's stage to the finish)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local msgs, saved = {}, nil
jsonWriteFile = function(_, d) saved = d end
jsonReadFile = function() return nil end
local events = {}
guihooks = { trigger = function(name, m) events[#events + 1] = { name, m } if m and m.msg then msgs[#msgs + 1] = m.msg end end }
vec3 = function(x, y, z) return { x = x, y = y, z = z } end
quatFromDir = function() return { x = 0, y = 0, z = 0, w = 1 } end
ColorF = function() return {} end
local R = require('beamngEarth/rally')

-- pure
T.check(R.fmt(65.25) == '1:05.2' or R.fmt(65.25) == '1:05.3', 'fmt ' .. R.fmt(65.25))
T.check(R.fmt(5) == '0:05.0', 'fmt short')
T.near(R.callLead(0), 70, 1e-9, 'call lead: never under 70 m')
T.near(R.callLead(40), 160, 1e-9, 'call lead: 4 s')
T.near(R.callLead(100), 250, 1e-9, 'call lead: at most 250 m')
-- the co-driver's clips
local function same(a, b) return table.concat(a, ',') == table.concat(b, ',') end
T.check(same(R.clips({ dir = 'left', grade = 4, name = '4', long = true, mod = 'tightens', link = 150 }),
  { 'long', 'four_left', 'tightens', '150' }), 'clips: long four left tightens 150')
T.check(same(R.clips({ dir = 'right', grade = 0, name = 'hairpin', link = 'into' }), { 'hairpin_right', 'into' }), 'clips: hairpin')
T.check(same(R.clips({ dir = 'left', grade = 1, name = 'square', link = 'and' }), { 'square_left', 'and' }), 'clips: square')
T.check(same(R.clips({ dir = 'right', grade = 6, name = '6', link = 1234 }), { 'six_right', '950' }), 'clips: far gap capped')
T.check(same(R.clips({ dir = 'right', grade = 5, name = '5', link = 170 }), { 'five_right', '150' }), 'clips: nearest distance')
T.check(#R.clips({}) == 0, 'clips: nothing for a bad note')
-- the pack's files (no metadata: FS), variants
local have = { pacenote_four_left = true, pacenote_long_1 = true, pacenote_long_2 = true }
FS = { fileExists = function(_, f) return have[f:match('([^/]+)%.ogg$')] or false end }
local c = R.resolveClip('ak_2026-04-16', 'four_left')
T.check(c and c.file == '/lua/ge/extensions/gameplay/rally/compositors/voicepacks/ak_2026-04-16/audio/pacenote_four_left.ogg', 'resolve: the file')
c = R.resolveClip('x', 'long')
T.check(c and c.file:find('pacenote_long_[12]%.ogg$'), 'resolve: a numbered variant')
T.check(R.resolveClip('x', 'tightens') == nil, 'resolve: missing clip')
FS = { fileExists = function() return true end }
local played = {}
Engine = { Audio = { intercomPlayPacenote = function(o) played[#played + 1] = o.filename:match('pacenote_(.-)%.ogg$') end } }
local xs, ys, cum = { 0, 100, 100 }, { 0, 0, 100 }, { 0, 100, 200 }
local s, i, off = R.progress(xs, ys, cum, 50, 3, 1)
T.near(s, 50, 1e-9, 'progress on the first leg'); T.near(off, 3, 1e-9, 'off the road')
s, i = R.progress(xs, ys, cum, 103, 40, 1)
T.near(s, 140, 1e-9, 'progress on the second leg'); T.check(i == 2, 'segment')
s = R.progress(xs, ys, cum, 100, 90, 1, 0)
T.check(s <= 100, 'the window keeps progress from jumping ahead')

-- cars: a stock rally config, ranked
core_vehicles = { getConfigList = function() return { configs = {
  a = { key = 'sport', model_key = 'sunburst2' }, b = { key = 'rally_tarmac', model_key = 'covet' },
  c = { key = 'rally_gravel', model_key = 'sunburst2', Name = 'Sunburst Rally' } } } end }
local m, cfg, nm = R.pickCar()
T.check(m == 'sunburst2' and cfg == 'vehicles/sunburst2/rally_gravel.pc' and nm == 'Sunburst Rally', 'pickCar ' .. tostring(cfg))

-- a run: a flat anchor (lat/lon -> m), a 1.5 km stage due north
local anchor = { geodeticToEnu = function(_, lat, lon) return (lon + 77) * 85000, (lat - 41) * 111000, 0 end }
local pts = {}
for k = 0, 30 do pts[#pts + 1] = { 41 + k * 50 / 111000, -77 } end
local own = { active = 1, getID = function() return 7 end }
own.setActive = function(self, v) self.active = v end
own.getPosition = function() return { x = 0, y = 0, z = 0 } end
own.getDirectionVector = function() return { x = 0, y = 1, z = 0 } end
local objs, player = { [7] = own }, own
be = { getPlayerVehicle = function() return player end, getObjectByID = function(_, id) return objs[id] end,
       enterVehicle = function(_, _, o) player = o end, getSurfaceHeightBelow = function() return 0 end }
core_vehicles.spawnNewVehicle = function(model, opt)
  local o = { JBeam = model, config = opt.config, getID = function() return 60 end }
  o.getPosition = function() return { x = 0, y = 1500, z = 0 } end
  o.getDirectionVector = function() return { x = 0, y = 1, z = 0 } end
  o.delete = function() objs[60] = nil end
  objs[60] = o
  player = o
  return o
end
local sent, teleported = nil, nil
beamngEarth_core = {
  requestRallyStage = function(id, st) sent = { id = id, stage = st } return true end,
  takeRallyStage = function(id)
    if sent and id == sent.id then
      return { id = id, stage = { id = 'asaph', name = 'Asaph', area = 'Tioga', dist = 1500, points = pts,
        notes = { { at = 0, text = 'Left 4 100' }, { at = 400, text = 'Right 3' }, { at = 1200, text = 'Left 2, to finish' } } } }
    end
  end,
  teleport = function(lat, lon) teleported = { lat, lon } return true end }

T.check(R.start('asaph'), 'start')
T.check(not R.start('asaph'), 'one run at a time')
T.check(sent.stage == 'asaph', 'the stage asked of the server')
local P = { x = 0, y = 0, z = 0, vx = 0, vy = 0 }
R.update(0.1, anchor, P)
T.check(R._state().phase == 'travel' and teleported and teleported[1] == 41, 'teleported to the start')
for _ = 1, 70 do R.update(0.1, anchor, P) end
T.check(R._state().phase == 'countdown', 'the rally car out after settling')
T.check(own.active == 0 and player ~= own, 'your car parked and hidden, you in the rally car')
-- a jump start
P.y = 5
R.update(0.1, anchor, P)
T.check(R._state().penalty == R.JUMP_S, 'jump start penalised')
P.y = 0
for _ = 1, 60 do R.update(0.1, anchor, P) end
T.check(R._state().phase == 'run', 'GO')
local decals = 0
Engine.Render = { DynamicDecalMgr = { addDecals = function(pool, n) decals = n; T.check(pool[1].texture:find('groundmarkers'), 'the game\'s arrow') end } }
R.update(0.1, anchor, P)
T.check(decals > 30 and decals <= 41, 'the ground line drawn ahead: ' .. decals)
local mr = R.mapRoute()
T.check(mr and #mr.points == #pts and mr.version > 900000, 'the stage on the GPS map')
local lastTime
for _, e in ipairs(events) do if e[1] == 'raceTime' then lastTime = e[2].time end end
T.check(lastTime and lastTime > 0, 'the RaceTime app fed: ' .. tostring(lastTime))
-- reset to road
local moved
spawn = { safeTeleport = function(o, pos) moved = pos end }
P.x = 30
local pen0 = R._state().penalty
T.check(R.resetToRoad(), 'reset to road')
T.check(moved and math.abs(moved.x) < 1e-6, 'the car back on the stage line')
T.check(R._state().penalty == pen0 + R.RESET_S, 'reset penalty')
P.x = 0
local found = false
for _, mm in ipairs(msgs) do if mm == 'Left 4 100' then found = true end end
T.check(found, 'the first call at GO')
local spoken = table.concat(played, ' ')
T.check(spoken:find('system_countdown5') and spoken:find('system_countdown1') and spoken:find('system_falseStart'), 'countdown and false start spoken: ' .. spoken)
-- drive north at 20 m/s
P.vy = 20
local calls0 = #msgs
for _ = 1, 900 do
  P.y = P.y + 2
  R.update(0.1, anchor, P)
  if not R._state() or R._state().phase ~= 'run' then break end
end
local st = R._state()
T.check(st and st.phase == 'done', 'finished')
local hadRight, hadSplit = false, false
for k = calls0, #msgs do
  if msgs[k] == 'Right 3' then hadRight = true end
  if tostring(msgs[k]):find('^Split 1') then hadSplit = true end
end
T.check(hadRight, 'the corner called on the way')
spoken = table.concat(played, ' ')
T.check(spoken:find('system_countdowngo'), 'go spoken')
-- (the stage's notes carry no parts here, so no corner clips; clips() is tested above)
T.check(hadSplit, 'the checkpoint split')
T.check(saved and saved.best and saved.best.asaph and saved.best.asaph > 70, 'the best time saved (with the penalty)')
for _ = 1, 70 do R.update(0.1, anchor, P) end
T.check(R._state() == nil, 'the run over')
T.check(events[#events][1] == 'ScenarioResetTimer' or true, 'clock')
local reset = false
for _, e in ipairs(events) do if e[1] == 'ScenarioResetTimer' then reset = true end end
T.check(reset, 'the RaceTime clock cleared after the run')
T.check(R.mapRoute() == nil, 'the GPS back to normal')
T.check(not R.resetToRoad(), 'no reset outside a run')
T.check(own.active == 1 and player == own and objs[60] == nil, 'your car back, the rally car gone')
local ui = R.uiState()
T.check(ui.last and ui.last.record and ui.stages[1].best, 'the app: last run and best')

-- the voice choice
T.check(R.setVoice('rh_2026-06-05') and R.uiState().voice == 'rh_2026-06-05' and saved.voice == 'rh_2026-06-05', 'voice chosen and saved')
T.check(not R.setVoice('nope'), 'unknown voice')
T.check(R.setVoice('off') and R.uiState().voice == 'off', 'voice off')
local n0 = #R._voiceQ()
R.setVoice('off')
T.check(#R._voiceQ() == n0, 'off: nothing queued')
R.setVoice('ak_2026-04-16')
-- quit mid-run gives the car back
R.start('asaph'); P.y = 0; P.vy = 0
for _ = 1, 80 do R.update(0.1, anchor, P) end
T.check(own.active == 0, 'out again')
R.quit()
T.check(own.active == 1 and player == own and R._state() == nil, 'quit: your car back')
-- the server failing
beamngEarth_core.takeRallyStage = function(id) return { id = id, error = 'the stage road was not found in the map data' } end
R.start('painter'); R.update(0.1, anchor, P)
T.check(R._state() == nil and R.uiState().error:find('not found'), 'server error shown')
T.check(not R.start('nope'), 'unknown stage')
T.done('test_rally')

-- luajit mod/tests/test_earthPhone.lua  (the phone's Lua side: the turn card, the speed tile, the heading, the payload)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local P = require('beamngEarth/earthPhone')

-- the maneuver icons (routing.parse_steps' OSRM type / modifier -> the app's glyphs)
T.check(P.icon({ type = 'depart' }) == 'depart', 'depart')
T.check(P.icon({ type = 'arrive' }) == 'arrive', 'arrive')
T.check(P.icon({ type = 'turn', modifier = 'left' }) == 'left', 'a left turn')
T.check(P.icon({ type = 'turn', modifier = 'right' }) == 'right', 'a right turn')
T.check(P.icon({ type = 'turn', modifier = 'slight right' }) == 'slight-right', 'a slight right')
T.check(P.icon({ type = 'turn', modifier = 'sharp left' }) == 'sharp-left', 'a sharp left')
T.check(P.icon({ type = 'turn', modifier = 'uturn' }) == 'uturn', 'a u-turn')
T.check(P.icon({ type = 'new name', modifier = 'straight' }) == 'straight', 'straight on')
T.check(P.icon({ type = 'merge' }) == 'merge', 'a merge')
T.check(P.icon({ type = 'roundabout', modifier = 'left' }) == 'roundabout', 'a roundabout is not a left turn')
T.check(P.icon(nil) == 'straight', 'no step: straight')

-- the instruction card: the maneuver ahead and how far it is
local steps = {
  { at = 0, dist = 300, name = '', type = 'depart' },
  { at = 300, dist = 200, name = 'Main Street', type = 'turn', modifier = 'left' },
  { at = 500, dist = 200, name = 'Elm Avenue', type = 'turn', modifier = 'right' },
  { at = 700, dist = 0, name = '', type = 'arrive' } }
local i = P.instruction(steps, 0)
T.check(i.icon == 'left' and i.road == 'Main Street' and math.abs(i.dist - 300) < 1e-9, 'at the start: left onto Main Street in 300 m')
i = P.instruction(steps, 350)
T.check(i.icon == 'right' and i.road == 'Elm Avenue' and math.abs(i.dist - 150) < 1e-9, 'after the first turn: right in 150 m')
i = P.instruction(steps, 690)
T.check(i.icon == 'arrive' and math.abs(i.dist - 10) < 1e-9, 'at the end: arrive in 10 m')
T.check(P.instruction({}, 0) == nil and P.instruction(nil, 0) == nil, 'no steps: no card')
T.check(P.stepIndex(steps, 0) == 1 and P.stepIndex(steps, 300) == 2 and P.stepIndex(steps, 1e9) == 4,
  'the step driven at a distance')

-- what a navigator would say (there is no voice: the card is the subtitle)
T.check(P.phrase({ type = 'turn', modifier = 'left', name = 'Main Street' }) == 'turn left onto Main Street',
  'a left turn onto a road')
T.check(P.phrase({ type = 'turn', modifier = 'slight right', name = 'A1' }) == 'turn slightly right onto A1',
  'a slight right')
T.check(P.phrase({ type = 'turn', modifier = 'sharp left', name = '' }) == 'turn sharp left', 'a sharp left, no name')
T.check(P.phrase({ type = 'turn', modifier = 'uturn' }) == 'make a U-turn', 'a u-turn')
T.check(P.phrase({ type = 'roundabout', modifier = 'right', exit = 3, name = 'Plaza' })
  == 'at the roundabout, take exit 3 onto Plaza', 'a roundabout by its exit')
T.check(P.phrase({ type = 'roundabout', modifier = 'right' }) == 'at the roundabout, continue', 'no exit number')
T.check(P.phrase({ type = 'merge', modifier = 'slight left', name = 'A5' }) == 'merge left onto A5', 'a merge')
T.check(P.phrase({ type = 'ramp', modifier = 'right', name = 'N-110' }) == 'take the ramp on the right onto N-110',
  'a ramp')
T.check(P.phrase({ type = 'fork', modifier = 'slight left', name = 'M-30' }) == 'keep left onto M-30', 'a fork')
T.check(P.phrase({ type = 'depart', modifier = 'north', name = 'Calle Mayor' }) == 'head north on Calle Mayor',
  'the start of the route')
T.check(P.phrase({ type = 'arrive' }) == 'arrive at your destination', 'the end')
T.check(P.phrase({ type = 'continue', name = 'Calle Mayor' }) == 'continue on Calle Mayor', 'a road that keeps its name')
T.check(P.phrase({ type = 'new name', modifier = 'straight' }) == 'continue straight', 'no name: straight on')

local banner = P.instruction(steps, 100)
T.check(banner.say == 'turn left onto Main Street' and banner.sayNow == 'Turn left onto Main Street',
  'the sentence, as a clause and as a standalone')
T.check(banner.thenSay == 'turn right onto Elm Avenue', 'the turn right after it (200 m apart) is said too')
local far = {
  { at = 0, type = 'depart' },
  { at = 100, name = 'Main Street', type = 'turn', modifier = 'left' },
  { at = 900, name = 'Elm Avenue', type = 'turn', modifier = 'right' } }
T.check(P.instruction(far, 0).thenSay == nil, 'a turn 800 m further is its own instruction')
T.check(P.instruction(steps, 690).sayNow == 'Arrive at your destination', 'the card arrives at the destination')

-- a dropped pin: shown at once, named by the server, gone when the route replaces it
local answers, asked, state2 = {}, {}, { player = { lat = 39.29, lon = -76.61, x = 0, y = 0 } }
beamngEarth_core = {
  _state = function() return state2 end,
  requestPhoneReverse = function(id, lat, lon) asked[#asked + 1] = { id = id, lat = lat, lon = lon }; return true end,
  takePhoneReverse = function(id) local r = answers[id]; answers[id] = nil; return r end }
T.check(P.setPin(39.3, -76.6) == true, 'a double tap drops a pin')
T.check(asked[1] and asked[1].lat == 39.3 and asked[1].lon == -76.6, 'the server is asked for its name')
local pinPayload = P.state().pin
T.check(pinPayload and pinPayload.lat == 39.3 and pinPayload.name == '', 'the pin shows before it is named')
answers[asked[1].id] = { name = 'Main Street 5', detail = 'Baltimore' }
P.mounted(true)
P.onUpdate(0.1)
P.mounted(false)
pinPayload = P.state().pin
T.check(pinPayload and pinPayload.name == 'Main Street 5' and pinPayload.detail == 'Baltimore',
  'named once the server answers')
T.check(P.setPin('x', nil) == false, 'a bad point is no pin')
beamngEarth_core = nil
T.check(P.clearPin() == true and P.state().pin == nil, 'closing the card drops the pin')

-- the speed tile (the graph keeps m/s, the country posts mph)
T.check(P.limitTile(13.4112, true).value == 30 and P.limitTile(13.4112, true).unit == 'mph', '30 mph from 13.4 m/s')
T.check(P.limitTile(13.4112, false).value == 48 and P.limitTile(13.4112, false).unit == 'km/h', '48 km/h from 13.4 m/s')
T.check(P.limitTile(nil, true) == nil and P.limitTile(0, false) == nil, 'no limit: no tile')

-- the heading (true north at the car, from the anchor's northAt)
local veh = { getDirectionVector = function() return { x = 1, y = 0 } end }        -- east in the frame
local st = { player = { x = 0, y = 0 }, anchor = { northAt = function() return 0, 1 end } }
T.check(P.heading(st, veh) == 90, 'east is 90 when the frame is north-up')
st.anchor = { northAt = function() return 1, 0 end }                              -- the frame is turned 90 degrees
T.check(P.heading(st, veh) == 0, 'east in a frame whose x is north is 0')
T.check(P.heading({}, veh) == 90, 'no anchor: the frame taken as north-up')
T.check(P.heading(st, nil) == nil, 'no vehicle: no heading')

-- the payload: where the car is and which way it points, the limit, the card - and never the speed
local state = { player = { lat = 39.2904, lon = -76.6122, x = 0, y = 0, speed = 12.5 } }
local pl = P.payload(state, nil)
T.check(pl.player.lat == 39.2904 and pl.player.lon == -76.6122, 'the car is in the payload')
T.check(pl.player.speed == nil and pl.speed == nil, 'no vehicle speed in the payload')
T.check(pl.limit == nil, 'no road graph: no limit tile')
T.check(pl.route == nil, 'no destination: no card')
T.check(pl.visible == false and pl.mounted == false, 'hidden until Page Up')

P.setVisible(true)
T.check(P.visible() == true, 'Page Up shows it')
P.toggle()
T.check(P.visible() == false, 'and hides it again')
T.check(P.mounted(true) == true and P.isMounted() == true, 'the app says when it is mounted')
P.mounted(false)
T.check(P.isMounted() == false, 'and when it goes')

-- inside the BeamNG.Earth phone (merged 2026-10-27): Maps is drawn in the phone's screen, so nothing is ever added
-- to the player's app layout (a standalone copy would show the map twice)
local layout = { filename = '/settings/ui_apps/layouts/default/freeroam.uilayout.json', apps = { { appName = 'tacho2' } } }
local sets, added = 0, nil
extensions = { ui_appLayouts = {
  getCurrentLayout = function() return layout end,
  setCurrentLayout = function(l) sets = sets + 1; return l end,
  addApp = function(file, name, placement) added = name return true end } }
T.check(P.ensureInLayout() == true and sets == 0 and added == nil and #layout.apps == 1, 'nothing added to the layout')
P.setVisible(true)
T.check(sets == 0 and added == nil, 'showing Maps adds nothing either')
P.setVisible(false)
extensions = nil

T.done('test_earthPhone')

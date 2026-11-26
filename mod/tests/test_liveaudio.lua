-- luajit mod/tests/test_liveaudio.lua
-- beamngEarth/liveaudio.lua (the Weather page's Weather sounds: Live / Pre-recorded): the message fields made safe, the
-- resend rule, the live check and its staleness (the pre-recorded sounds come back when the server stops answering),
-- the page's line, the game's volumes, then the runtime: pre-recorded said once then nothing, live heartbeats and
-- thunders, the fade of the mod's own sounds, a reset.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local la = require('beamngEarth/liveaudio')
local check, near = T.check, T.near

-- fields: numbers clamped, junk replaced
local f = la.fields({ rainMmH = -3, windMs = 0 / 0, shelter = 4, tunnel = 1, cabin = true, volume = 2, duck = -1 })
check(f.rainMmH == 0 and f.windMs == 0 and f.shelter == 1 and f.tunnel == false and f.cabin == true, 'fields: clamped')
check(f.volume == 1 and f.thunderVolume == 1 and f.duck == 0 and f.muted == false and f.t == 0, 'fields: defaults')
f = la.fields({ rainMmH = 4.2, volume = 0.5, thunderVolume = 0.25 })
check(f.rainMmH == 4.2 and f.volume == 0.5 and f.thunderVolume == 0.25, 'fields: kept')

-- the signature: rounding jitter does not change it, a real change does
local s1 = la.signature('live', la.fields({ rainMmH = 4.201 }))
check(s1 == la.signature('live', la.fields({ rainMmH = 4.204 })), 'signature: jitter')
check(s1 ~= la.signature('live', la.fields({ rainMmH = 4.3 })), 'signature: a change')
check(s1 ~= la.signature('prerecorded', la.fields({ rainMmH = 4.201 })), 'signature: the mode')

-- the send rule
check(la.shouldSend('prerecorded', 'a', nil, 0, nil, 0), 'pre-recorded: the first')
check(not la.shouldSend('prerecorded', 'a', 'a', 100, 0, 0), 'pre-recorded: never again unchanged')
check(la.shouldSend('live', 'a', 'a', 1.0, 0.7, 0), 'live: the heartbeat after SEND_S')
check(not la.shouldSend('live', 'a', 'a', 1.0, 0.9, 0), 'live: not before')
check(la.shouldSend('live', 'a', 'a', 1.0, 0.99, 1), 'live: a thunder at once')
check(la.shouldSend('live', 'b', 'a', 1.0, 0.99, 0), 'live: a change at once')

-- a thunder: the hint and the final one share the id; at = the strike + the sound's delay
local th = la.thunder({ t = 1000.5, seed = 77 }, 1715, { delay = 5, kind = 'close', gain = 0.6 }, false)
check(th.id == '77@1000.500' and th.at == 1005.5 + la.FILE_ONSET_S and th.d == 1715 and th.kind == 'close' and th.final == false,
  'thunder: hint (generated: the game plays the file, its crack FILE_ONSET_S in)')
check(not la.liveThunder() and la.thunderSet() == 'generated', 'generated: the game plays the thunder')
la.setThunderSet('recorded')
check(not la.liveThunder(), 'recorded, but the synth has not said it plays it: the files')
la.onAudioState({ state = 'live', thunderSets = { 'recorded' } })
check(la.liveThunder() and la.thunder({ t = 1000.5, seed = 77 }, 1715, { delay = 5, kind = 'close', gain = 0.6 }, false).at == 1005.5,
  'recorded, offered by the synth: the synth plays it, its crack at once')
la.onAudioState({ state = 'live', thunderSets = {} })
check(not la.liveThunder(), 'a synth without the bank: the files again')
la.reset()
check(la.fields({ thunderSet = 'recorded' }).thunderSet == 'recorded' and la.fields({}).thunderSet == 'generated', 'fields: the set')
la.setThunderSet('generated')
check(la.thunder({ t = 1000.5, seed = 77 }, 1700, { delay = 4.9, kind = 'close', gain = 0.6 }, true).id == th.id, 'thunder: same id')
check(la.thunderId({ t = 1000.5, seed = 77 }) == th.id, 'thunderId')

-- the generated set live: method B's strikes and closes when the synth has them ready, the files otherwise
la.setThunderSet('generated')
check(not la.synthPlays('close', th.id, 1715, false), 'generated, the synth not offering it: the files')
la.onAudioState({ state = 'live', thunderSets = { 'generated', 'recorded' }, readyThunders = { { th.id, 1715 }, 'junk', { 'x' } } })
check(la.synthPlays('close', th.id, 1715, false) and la.synthPlays('strike', 'other', 100, false), 'a strike / close hint: rendered')
check(not la.synthPlays('mid', 'm', 3000, false) and not la.synthPlays('far', 'f', 9000, true), 'mid / far: the files')
check(la.synthPlays('close', th.id, 1715 * 1.1, true), 'a final within 15 % of the render: the synth plays it')
check(not la.synthPlays('close', th.id, 1715 * 1.2, true), 'moved further: the file')
check(not la.synthPlays('close', 'not-ready', 1715, true), 'not ready: the file')
local fs = la.thunder({ t = 1000.5, seed = 77 }, 1715, { delay = 5, kind = 'close', gain = 0.6 }, true, true)
check(fs.play == true and fs.at == 1005.5, 'final, the synth plays: play, the crack at once')
local ff = la.thunder({ t = 1000.5, seed = 77 }, 1715, { delay = 5, kind = 'close', gain = 0.6 }, true, false)
check(ff.play == false and ff.at == 1005.5 + la.FILE_ONSET_S, 'final, the game plays: play false, at its file\'s crack')
check(la.thunder({ t = 1000.5, seed = 77 }, 1715, { delay = 5, kind = 'close', gain = 0.6 }, false, true).play == nil, 'a hint has no play')
la.setThunderSet('recorded')
check(la.synthPlays('mid', 'm', 3000, true), 'recorded: the synth plays every kind')
la.reset()
la.setThunderSet('generated')

-- live only while the server's last word is live and fresh
check(la.liveNow('live', { state = 'live', t = 10 }, 11), 'live: fresh')
check(not la.liveNow('live', { state = 'live', t = 10 }, 10 + la.STALE_S + 0.01), 'live: stale')
check(not la.liveNow('prerecorded', { state = 'live', t = 10 }, 10.1), 'live: the setting')
check(not la.liveNow('live', { state = 'failed', t = 10 }, 10.1), 'live: failed')

-- the page's line
check(la.statusText('prerecorded', nil, 0) == 'Pre-recorded', 'line: pre-recorded')
check(la.statusText('live', { state = 'live', t = 1, latencyMs = 23.4 }, 1.2) == 'Live (23 ms output latency)', 'line: live')
check(la.statusText('live', { state = 'failed', reason = 'no audio device', t = 1 }, 1.2):find('no audio device', 1, true) ~= nil,
  'line: failed, with the reason')
check(la.statusText('live', { state = 'live', t = 1 }, 9):find('stopped answering', 1, true) ~= nil, 'line: stale')
check(la.statusText('live', { state = 'off', reason = 'no weathersynth binary', t = 1 }, 1.1):find('not available', 1, true) ~= nil,
  'line: off with a reason')

-- the game's volumes: runtime channels first, the saved settings else, master x channel
local chan = { AudioChannelMaster = 0.8, AudioChannelOther = 0.5 }
near(la.channelVolume(function(c) return chan[c] end, nil, 'AudioChannelOther', 'AudioOtherVol'), 0.4, 1e-12, 'volume: channels')
local saved = { AudioMasterVol = 0.5, AudioOtherVol = 0.5 }
near(la.channelVolume(nil, function(k) return saved[k] end, 'AudioChannelOther', 'AudioOtherVol'), 0.25, 1e-12, 'volume: settings')
near(la.channelVolume(function() error('no') end, nil, 'AudioChannelOther', 'AudioOtherVol'), 1, 1e-12, 'volume: neither: full')
near(la.channelVolume(function(c) return c == 'AudioChannelMaster' and 3 or 0.5 end, nil, 'x', 'y'), 0.5, 1e-12, 'volume: clamped')
-- the rain's and the thunder's volume is the Environment slider's (storm.THUNDER_DESC and ambience.LAYER_GROUP play
-- there), not Other's, the UI's or the missing Gui channel's (whose getChannelVolume answers 1)
do
  local oldEngine = rawget(_G, 'Engine')
  local vols = { AudioChannelMaster = 0.5, AudioChannelOther = 0.6, AudioChannelEnvironment = 0.4, AudioChannelUi = 0.9,
    AudioChannelGui = 1 }
  _G.Engine = { Audio = { getChannelVolume = function(c) return vols[c] or 1 end } }
  local rain, thunder = la.gameVolumes()
  _G.Engine = oldEngine
  near(rain, 0.2, 1e-12, 'game volumes: rain = master x Environment (the layers play there)')
  near(thunder, 0.2, 1e-12, 'game volumes: thunder = master x Environment')
  check(require('beamngEarth/storm').THUNDER_DESC == 'AudioEnvironment', 'the thunder files play through AudioEnvironment')
end

-- the runtime
local sent = {}
local connected = true
la.setSender(function(m) if not connected then return false end sent[#sent + 1] = m return true end)
la.tick(0, nil)
check(#sent == 1 and sent[1].type == 'weather_audio' and sent[1].mode == 'prerecorded', 'pre-recorded: said once')
for i = 1, 50 do la.tick(i * 0.016, nil) end
check(#sent == 1, 'pre-recorded: then nothing')
la.onThunder({ id = 'x' })
check(#sent == 1, 'pre-recorded: thunders ignored')

la.setMode('live')
la.tick(1.0, { rainMmH = 3, volume = 0.7, thunderVolume = 0.6 })
check(#sent == 2 and sent[2].mode == 'live' and sent[2].rainMmH == 3 and sent[2].volume == 0.7, 'live: sent at once')
check(sent[2].thunders == nil, 'live: no thunders, no list')
la.tick(1.1, { rainMmH = 3, volume = 0.7, thunderVolume = 0.6 })
check(#sent == 2, 'live: not before the heartbeat')
la.tick(1.3, { rainMmH = 3, volume = 0.7, thunderVolume = 0.6 })
check(#sent == 3, 'live: the heartbeat')
la.onThunder(th)
la.tick(1.31, { rainMmH = 3, volume = 0.7, thunderVolume = 0.6 })
check(#sent == 4 and sent[4].thunders and sent[4].thunders[1].id == th.id, 'live: a thunder at once')
la.tick(1.6, { rainMmH = 3, volume = 0.7, thunderVolume = 0.6 })
check(#sent == 5 and sent[5].thunders == nil, 'live: a thunder is sent once')
connected = false
la.onThunder(th)
la.tick(1.62, { rainMmH = 3 })
check(#sent == 5, 'live: disconnected, nothing')
connected = true
la.tick(1.63, { rainMmH = 3 })
check(#sent == 6 and sent[6].thunders and #sent[6].thunders == 1, 'live: the thunder kept for the reconnect')
for i = 1, la.MAX_PENDING + 5 do la.onThunder({ id = tostring(i) }) end
la.tick(1.64, { rainMmH = 3 })
check(#sent[7].thunders == la.MAX_PENDING and sent[7].thunders[1].id == '6', 'live: pending capped, the oldest dropped')

-- Live's rain is the game's own (the default since the 2026-11-26 playtest, LIVE_RAIN false): the synth answers live,
-- the mod's rain stays full, and every message tells the synth to keep its rain off
check(la.LIVE_RAIN == false, 'Live: the game\'s rain loops by default')
check(sent[2].synthRain == false and sent[7].synthRain == false, 'Live: synthRain = false in every message')
do
  local t0 = os.clockhp and os.clockhp() or os.clock()
  la.onAudioState({ state = 'live', latencyMs = 20 })
  local f
  for i = 1, 20 do f = la.ownFactor(t0 + i * 0.05) end
  check(la.isLive(t0 + 0.5) and f == 1, 'Live, the synth answering: the mod\'s rain stays full (the synth only thunders)')
  la.onAudioState({ state = 'off' })   -- (the server's word gone again for the fade below)
end
la.LIVE_RAIN = true     -- (the synth's rain, for when the recorded-mix rain lands: the handover fade below)

-- the mod's own sounds: full until the server says live, faded out over FADE_S, back when it goes stale
local now0 = os.clockhp and os.clockhp() or os.clock()
check(la.ownFactor(now0 - 1) == 1, 'own: full before the server answers')
check(la.ownFactor(now0) == 1, 'own: still full')
la.onAudioState({ state = 'live', latencyMs = 20 })
near(la.ownFactor(now0 + 0.05), 1 - 0.05 / la.FADE_S, 1e-9, 'own: fading')
near(la.ownFactor(now0 + 0.1), 1 - 0.1 / la.FADE_S, 1e-9, 'own: fading on')
near(la.ownFactor(now0 + 0.4), 1 - 0.2 / la.FADE_S, 1e-9, 'own: a long frame counts 0.1 s at most')
near(la.ownFactor(now0 + 0.5), 0, 1e-9, 'own: silent while live')
check(la.ownFactor(now0 - 5) == 0, 'own: a clock step back changes nothing')
check(la.ownFactor(now0 + la.STALE_S + 0.2) > 0, 'own: back when the server stops answering')
-- many frames of odd lengths (and a clock far from 0, where sums of steps round): the fade ends exactly at 0 / 1
do
  local saved = os.clockhp
  local t = 98765.4321
  os.clockhp = function() return t end
  local odd = { 0.0137, 0.0161, 0.00833, 0.0219, 0.0071, 0.0333 }
  local function frames(n, live)
    local f
    for i = 1, n do
      t = t + odd[i % #odd + 1]
      if live then la.onAudioState({ state = 'live', latencyMs = 20 }) end
      f = la.ownFactor(t)
    end
    return f
  end
  -- three 0.1 s frames make exactly FADE_S, but differences of a large clock round: without the snap the fade stopped
  -- a hair short of 1 (the flake: 'a clock step back' then found 1e-16, not 0)
  for k = 1, 20 do
    t = 98765.4321 + k * 13.37
    la.onAudioState({ state = 'live', latencyMs = 20 })
    t = t + la.STALE_S + 1
    for _ = 1, 6 do t = t + 0.1; la.ownFactor(t) end   -- (stale: the mod's sounds back to full, fade exactly 0)
    la.onAudioState({ state = 'live', latencyMs = 20 })
    local base = t
    for _, d in ipairs({ 0.1, 0.2, 0.3 }) do t = base + d; la.ownFactor(t) end
    local f = la.ownFactor(t)                   -- (dt 0)
    check(f == 0, 'three frames of FADE_S / 3 at clock ' .. k .. ': exactly silent: ' .. string.format('%.17g', f))
  end
  for round = 1, 5 do
    local f = frames(97 + round, true)
    check(f == 0, 'odd frames, live: the mod\'s own sounds exactly silent: ' .. string.format('%.17g', f))
    t = t + la.STALE_S + 0.01
    f = frames(89 + round, false)
    check(f == 1, 'odd frames, stale: exactly full again: ' .. string.format('%.17g', f))
  end
  os.clockhp = saved
end
-- active / due: what ambience.lua does each frame
check(la.active() and la.due(1000) and not la.due(1.65), 'live: active; a message due SEND_S after the last, not before')
check(la.status(now0).live and la.status(now0).text:find('Live', 1, true) == 1, 'status: live')

-- a reset (a new connection): the mode said again, the server's word forgotten
la.reset()
check(not la.isLive(now0), 'reset: not live')
local n = #sent
la.tick(5, { rainMmH = 0 })
check(#sent == n + 1 and sent[#sent].mode == 'live', 'reset: said again')
la.setMode('prerecorded')
check(la.due(5.01), 'back to pre-recorded: due')
la.tick(5.01, nil)
check(sent[#sent].mode == 'prerecorded', 'back to pre-recorded: said at once')
check(not la.due(5.02), 'pre-recorded said: nothing due')
for i = 1, 40 do la.ownFactor(10 + i * 0.05) end
check(not la.active(), 'pre-recorded, said and faded back: inactive (ambience skips it all)')
la.setSender(nil)
la.setMode('live')
check(la.active() and not la.due(20), 'live but disconnected: active (the fade), nothing due')

T.done('test_liveaudio')

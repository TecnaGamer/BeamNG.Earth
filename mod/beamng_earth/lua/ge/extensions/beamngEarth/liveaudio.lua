-- BeamNG.Earth: live weather sounds (the user, 2026-11-25: "You can have a setting switch between live and pre-recorded").
--
-- With the Weather page's "Weather sounds: Live" the world server plays the rain and the thunder itself (native/
-- weathersynth, out of the default audio device, server/liveaudio.py), from what this module tells it: the
-- 'weather_audio' message (the rain rate, the wind, under a roof / in a tunnel / in the car, the game's own volumes,
-- whether to be quiet, and the thunders storm.lua schedules). The mod's own rain layers and thunder files fall silent
-- ONLY while the server says it is live ('audio_state' live, at most STALE_S old): a server without the live path, a
-- failed audio device, a server restart or a lost connection bring the pre-recorded sounds back within STALE_S.
-- Pre-recorded (the default until live has been checked in game): one 'weather_audio' with mode = 'prerecorded' after
-- a change or a connect, so the server stops its own; after that nothing per frame but two comparisons.
local M = {}

M.SEND_S = 0.25          -- weather_audio while live (and at once on a change or a thunder)
M.STALE_S = 1.5          -- a live audio_state older than this: the pre-recorded sounds come back
M.FADE_S = 0.3           -- the handover fade (the mod's sounds out / back in)
M.MAX_PENDING = 32       -- thunders kept for the next message
-- Who plays the thunder while Live, per thunder set (and only when the synth lists the set in thunderSets): true the
-- synth, a new render per strike (Recorded: the grain bank, tens of ms); 'ready' the synth for LIVE_KINDS when it
-- rendered the strike in time (Generated: method B's strikes and closes take seconds, so storm.lua hints them
-- LIVE_LOOKAHEAD_S ahead and the synth lists them in readyThunders), the game's files otherwise; false the game (the
-- mod's files, as in Pre-recorded; the synth then only ducks its rain at each strike's crack).
M.LIVE_THUNDER = { generated = 'ready', recorded = true }
M.LIVE_KINDS = { strike = true, close = true }
-- Whose rain plays while Live, per Rain sound: false the game's own rain loops, exactly as in Pre-recorded (the synth's
-- rain is off: synthRain = false; the user, 2026-11-26, of the procedural live drops: "the rain drops with Live sounds
-- sound terrible and are super lound"); true the synth's, the game's fading out while the synth answers, and only when
-- the synth lists the set in rainSets (the recorded rain needs its texture bank: without it the game plays the
-- recorded FILES, not the synth's procedural rain). The synth keeps the thunder and its ducks either way.
-- Generated is method B's live engine since 2026-11-26 (rainb_live.rs; the user, of its sweep: "Sweep sounds good"):
-- the synth plays it only when it lists 'generated_b' (LIVE_RAIN_NEEDS: an older synth's 'generated' is the procedural
-- drops of before, which stay off).
M.LIVE_RAIN = { generated = true, recorded = true }
M.LIVE_RAIN_NEEDS = { generated = 'generated_b', recorded = 'recorded' }
-- The same for the wind, per Wind sound: the synth plays no wind yet, so the game's wind files play in both modes (the
-- hook: a set turned true here, and listed by the synth, would hand that set to it as LIVE_RAIN does)
M.LIVE_WIND = { generated = false, recorded = false }
-- a ready render serves a final whose distance is within this much of the one it was rendered for (the synth's
-- READY_REL_D: a hint moved further renders again)
M.READY_REL_D = 0.15
-- the crack's place in the thunder files (storm.FILE_ONSET_S): a thunder the game plays arrives this late in its file
M.FILE_ONSET_S = 0.5

local mode = 'prerecorded'   -- the setting: 'prerecorded' | 'live'
local state = { state = 'off', t = nil }   -- the server's last audio_state and when it came (os.clockhp)
local lastSig, lastSendT, modeSent = nil, nil, nil
local fade = 0               -- 0: the mod's sounds full, 1: silent (live)
local fadeAt = nil
local pending = {}           -- thunders for the next message
local sender = nil           -- core.lua's: send(msg) -> true when sent (connected, a session)
local thunderSet = 'generated' -- the Weather page's Thunder sound: 'generated' | 'recorded'
local rainSet = 'generated'    -- the Weather page's Rain sound (ambience.setRainSet tells it)

function M.setRainSet(v)
  local s = (v == 'recorded') and 'recorded' or 'generated'
  rainSet = s   -- (in the signature: the next tick sends it)
end
function M.rainSet() return rainSet end
-- Whether the synth plays the rain now: the set's LIVE_RAIN, and the synth offers that set (rainSets: its token in
-- LIVE_RAIN_NEEDS).
function M.synthRain()
  local need = M.LIVE_RAIN_NEEDS[rainSet] or rainSet
  return M.LIVE_RAIN[rainSet] == true and type(state.rainSets) == 'table' and state.rainSets[need] == true
end

local function clockhp() return os.clockhp and os.clockhp() or os.clock() end

------------------------------------------------------------------------------------------
-- pure

-- Pure: a context { rainMmH, windMs, shelter 0..1, tunnel, cabin, volume, thunderVolume, muted, duck 0..1, t } made
-- safe (numbers, clamped).
function M.fields(ctx)
  ctx = ctx or {}
  local num = function(v, d) v = tonumber(v) if v == nil or v ~= v then return d end return v end
  local c01 = function(v, d) return math.max(0, math.min(1, num(v, d))) end
  return {
    rainMmH = math.max(0, num(ctx.rainMmH, 0)), windMs = math.max(0, num(ctx.windMs, 0)),
    shelter = c01(ctx.shelter, 1), tunnel = ctx.tunnel == true, cabin = ctx.cabin == true,
    volume = c01(ctx.volume, 1), thunderVolume = c01(ctx.thunderVolume, 1),
    muted = ctx.muted == true, duck = c01(ctx.duck, 1), t = num(ctx.t, 0),
    thunderSet = (ctx.thunderSet == 'recorded') and 'recorded' or 'generated',
    -- the Rain sound ('recorded': the synth's texture-bank rain) and the mod's own rain volume for its layers (RAIN_GAIN x
    -- the rain share), so the synth plays at the files' level
    rainSet = (ctx.rainSet == 'recorded') and 'recorded' or 'generated',
    rainVol = c01(ctx.rainVol, 0.5),
    -- the roof outside near the car (0..1) and whether a shelter is enclosed (muffled A) or open (B)
    roofNear = c01(ctx.roofNear, 0), enclosed = ctx.enclosed ~= false,
  }
end

-- Pure: what decides a resend (rounded, so jitter does not send every frame).
function M.signature(m, f)
  return string.format('%s|%.2f|%.1f|%.2f|%s|%s|%.2f|%.2f|%s|%.2f|%s|%s|%s|%.2f|%.1f|%s', m, f.rainMmH, f.windMs,
    f.shelter, tostring(f.tunnel), tostring(f.cabin), f.volume, f.thunderVolume, tostring(f.muted), f.duck,
    tostring(f.thunderSet), tostring(M.synthRain()), tostring(f.rainSet), f.rainVol, f.roofNear, tostring(f.enclosed))
end

-- Pure: the id of strike s ({ t, seed }): its hint and its final one share it.
function M.thunderId(s)
  return string.format('%d@%.3f', tonumber(s.seed) or 0, tonumber(s.t) or 0)
end

-- Pure: a thunder for the synth from storm.lua's strike s ({ t, seed }), its distance d m and storm.thunderFor(d) th:
-- id, at (UTC s its crack arrives), d, kind, gain, seed, final (false: a look-ahead hint to render early; true: it
-- struck) and, on a final, play (synth: who plays it, M.synthPlays; nil: as liveThunder says).
function M.thunder(s, d, th, final, synth)
  local t, seed = tonumber(s.t) or 0, tonumber(s.seed) or 0
  if synth == nil then synth = M.liveThunder() end
  -- (a thunder the game plays from a file: its crack comes FILE_ONSET_S into it, which is when the synth's rain ducks)
  local lead = synth and 0 or M.FILE_ONSET_S
  local m = { id = M.thunderId(s), at = t + (tonumber(th.delay) or 0) + lead, d = tonumber(d) or 0,
    kind = th.kind, gain = tonumber(th.gain) or 0, seed = seed, final = final == true }
  if final == true then m.play = synth == true end
  return m
end

function M.setThunderSet(v)
  local s = (v == 'recorded') and 'recorded' or 'generated'
  if s ~= thunderSet then
    thunderSet = s
    lastSig = nil
  end
end
function M.thunderSet() return thunderSet end
-- Whether the synth plays the thunder now (Live, and the set's LIVE_THUNDER).
-- (and only when the synth says it plays that set: a synth without its grain bank leaves the thunder to the files)
function M.liveThunder()
  return M.LIVE_THUNDER[thunderSet] == true and type(state.sets) == 'table' and state.sets[thunderSet] == true
end

-- Whether the synth has thunder id rendered for about distance d (its last readyThunders).
function M.readyFor(id, d)
  local r = type(state.ready) == 'table' and state.ready[id] or nil
  return r ~= nil and math.abs(r - (tonumber(d) or 0)) <= M.READY_REL_D * math.max(r, 1)
end

-- Whether the synth plays a thunder of kind (final) or should render it (a hint, final false): the set offered by
-- the synth, and for a 'ready' set a strike or close it listed ready (a hint: always, so it renders).
function M.synthPlays(kind, id, d, final)
  if type(state.sets) ~= 'table' or state.sets[thunderSet] ~= true then return false end
  local lt = M.LIVE_THUNDER[thunderSet]
  if lt == true then return true end
  if lt ~= 'ready' or not M.LIVE_KINDS[kind] then return false end
  if not final then return true end
  return M.readyFor(id, d)
end

-- Pure: send now? (a changed signature or a waiting thunder at once; while live also every SEND_S, the heartbeat the
-- server's synth and state ride on)
function M.shouldSend(m, sig, prevSig, now, prevT, nPending)
  if m ~= 'live' then return sig ~= prevSig end          -- (pre-recorded: once after a change)
  if sig ~= prevSig or (nPending or 0) > 0 then return true end
  return prevT == nil or now - prevT >= M.SEND_S
end

-- Pure: is the live path playing now? (the setting live, the server's last word live and fresh)
function M.liveNow(m, st, now)
  return m == 'live' and type(st) == 'table' and st.state == 'live' and st.t ~= nil and now - st.t <= M.STALE_S
end

-- Pure: the Weather page's line.
function M.statusText(m, st, now)
  if m ~= 'live' then return 'Pre-recorded' end
  if M.liveNow(m, st, now) then
    local lat = tonumber(st.latencyMs)
    return lat and string.format('Live (%d ms output latency)', math.floor(lat + 0.5)) or 'Live'
  end
  if type(st) == 'table' and st.state == 'failed' then
    return 'Pre-recorded (live failed: ' .. tostring(st.reason or 'unknown') .. ')'
  end
  if type(st) == 'table' and st.state == 'live' then return 'Pre-recorded (the live sound stopped answering)' end
  if type(st) == 'table' and st.state == 'off' and st.reason then
    return 'Pre-recorded (live not available: ' .. tostring(st.reason) .. ')'
  end
  return 'Pre-recorded (waiting for the live sound)'
end

-- Pure: the game's volume for a channel 0..1, master x channel: the runtime channel volumes (they follow the options
-- page) or else the saved settings.
function M.channelVolume(getChannel, getSetting, channel, settingKey)
  local function try(f, ...)
    if type(f) ~= 'function' then return nil end
    local ok, v = pcall(f, ...)
    v = ok and tonumber(v) or nil
    if v == nil or v ~= v then return nil end
    return math.max(0, math.min(1, v))
  end
  local master = try(getChannel, 'AudioChannelMaster') or try(getSetting, 'AudioMasterVol') or 1
  local ch = try(getChannel, channel) or try(getSetting, settingKey) or 1
  return master * ch
end

------------------------------------------------------------------------------------------
-- runtime

function M.setMode(v)
  local m = (v == 'live') and 'live' or 'prerecorded'
  if m ~= mode then
    mode = m
    lastSig = nil                  -- (the change is sent at once)
    if m ~= 'live' then pending = {} end
  end
end
function M.mode() return mode end
function M.setSender(fn) sender = fn end

-- storm.lua: a strike's thunder (M.thunder: a hint at look-up, the final one when it strikes). Only while live.
function M.onThunder(t)
  if mode ~= 'live' or type(t) ~= 'table' then return end
  if #pending >= M.MAX_PENDING then table.remove(pending, 1) end
  pending[#pending + 1] = t
end

-- core.lua: the server's 'audio_state' { state = 'live' | 'off' | 'failed', reason?, latencyMs? }
function M.onAudioState(msg)
  if type(msg) ~= 'table' then return end
  local sets = {}
  if type(msg.thunderSets) == 'table' then
    for _, v in ipairs(msg.thunderSets) do sets[tostring(v)] = true end
  end
  -- readyThunders: [[id, d], ...], the strikes the synth has rendered and waiting
  local ready = {}
  if type(msg.readyThunders) == 'table' then
    for i, v in ipairs(msg.readyThunders) do
      if i > 32 then break end
      if type(v) == 'table' and v[1] ~= nil and tonumber(v[2]) then ready[tostring(v[1])] = tonumber(v[2]) end
    end
  end
  local rainSets = {}
  if type(msg.rainSets) == 'table' then
    for _, v in ipairs(msg.rainSets) do rainSets[tostring(v)] = true end
  end
  state = { state = tostring(msg.state or 'off'), reason = msg.reason, latencyMs = tonumber(msg.latencyMs), t = clockhp(),
    sets = sets, ready = ready, rainSets = rainSets }
end

-- A new connection, its end or a level change: the server's word is gone, the pre-recorded sounds come back, and the
-- mode is said again.
function M.reset()
  state = { state = 'off', t = nil }
  lastSig, lastSendT, modeSent = nil, nil, nil
  pending = {}
end

function M.isLive(now) return M.liveNow(mode, state, now or clockhp()) end

-- Anything to do this frame? (Live, a Pre-recorded not said yet, or the mod's own sounds still fading back.) False
-- in Pre-recorded once said and faded: ambience.lua skips the rest.
function M.active() return mode == 'live' or (sender ~= nil and modeSent ~= 'prerecorded') or fade > 0 end

-- A message due now? (Pre-recorded: once, not said yet; Live: a thunder waiting, or SEND_S since the last.) The
-- caller builds the context (the tunnel walk, the volumes) only then.
function M.due(now)
  if not sender then return false end
  if mode ~= 'live' then return modeSent ~= 'prerecorded' end
  return #pending > 0 or lastSig == nil or lastSendT == nil or now - lastSendT >= M.SEND_S
end

-- The mod's own rain's factor (1 full .. 0 silent), faded over FADE_S between the two: silent only while the synth
-- plays the rain (LIVE_RAIN) and answers.
function M.ownFactor(now)
  now = now or clockhp()
  local target = (M.synthRain() and M.isLive(now)) and 1 or 0
  local dt = fadeAt and math.max(0, math.min(now - fadeAt, 0.1)) or 0   -- (not called while inactive: no jump back)
  fadeAt = now
  local step = dt / M.FADE_S
  fade = fade < target and math.min(target, fade + step) or math.max(target, fade - step)
  if math.abs(fade - target) < 1e-6 then fade = target end   -- (sums of steps stop just short: exactly 0 / 1)
  return 1 - fade
end

-- The game's volumes: rain and thunder on the Environment channel, the one the mod's own play on (the thunder files
-- through storm.THUNDER_DESC 'AudioEnvironment', the rain layers with ambience.LAYER_GROUP; core/settings/audio.lua:
-- AudioChannelEnvironment, pref AudioEnvironmentVol).
M.RAIN_CHANNEL = { 'AudioChannelEnvironment', 'AudioEnvironmentVol' }
M.THUNDER_CHANNEL = { 'AudioChannelEnvironment', 'AudioEnvironmentVol' }
function M.gameVolumes()
  local au = Engine and Engine.Audio
  local getChannel = au and au.getChannelVolume and function(c) return au.getChannelVolume(c, false) end or nil
  local s = rawget(_G, 'settings')
  local getSetting = s and s.getValue and function(k) return s.getValue(k) end or nil
  return M.channelVolume(getChannel, getSetting, M.RAIN_CHANNEL[1], M.RAIN_CHANNEL[2]),
    M.channelVolume(getChannel, getSetting, M.THUNDER_CHANNEL[1], M.THUNDER_CHANNEL[2])
end

function M.paused()
  local sta = rawget(_G, 'simTimeAuthority')
  if sta and sta.getPause then
    local ok, p = pcall(sta.getPause)
    return ok and p == true
  end
  return false
end

-- Quiet now? The game paused, or its window unfocused while the game's own "mute when unfocused" is on.
function M.quiet()
  if M.paused() then return true end
  local s = rawget(_G, 'settings')
  local muteUnfocused = s and s.getValue and s.getValue('AudioMuteOnWindowLoseFocus')
  if muteUnfocused == true or muteUnfocused == 1 then
    local focused = rawget(_G, 'isWindowFocused') or (Engine and Engine.isWindowFocused)
    if type(focused) == 'function' then
      local ok, f = pcall(focused)
      if ok and f == false then return true end
    end
  end
  return false
end

-- The UTC clock the thunders' times are on (storm.lua's: os.time steadied by the high-resolution clock).
function M.clock()
  local storm = package.loaded['beamngEarth/storm']
  if storm and storm.now then return storm.now() end
  return os.time()
end

-- From ambience.update when M.due (every SEND_S while live, or a thunder waiting). ctx as M.fields' (the volumes and
-- the quiet reasons filled here while live).
function M.tick(now, ctx)
  if mode ~= 'live' and modeSent == 'prerecorded' then return end   -- (pre-recorded and said so: nothing)
  local send = sender
  if not send then return end
  ctx = ctx or {}
  if mode == 'live' then
    if ctx.volume == nil then ctx.volume, ctx.thunderVolume = M.gameVolumes() end
    ctx.muted = ctx.muted == true or M.quiet()
    ctx.t = ctx.t or M.clock()
  end
  ctx.thunderSet = thunderSet
  local f = M.fields(ctx)
  local sig = M.signature(mode, f)
  if not M.shouldSend(mode, sig, lastSig, now, lastSendT, #pending) then return end
  local th = #pending > 0 and pending or nil
  local ok, sent = pcall(function()
    return send({ type = 'weather_audio', mode = mode, rainMmH = f.rainMmH, windMs = f.windMs,
      shelter = f.shelter, tunnel = f.tunnel, cabin = f.cabin, volume = f.volume, thunderVolume = f.thunderVolume,
      muted = f.muted, duck = f.duck, t = f.t, thunderSet = f.thunderSet, synthRain = M.synthRain(),
      rainSet = f.rainSet, rainVol = f.rainVol, roofNear = f.roofNear, enclosed = f.enclosed,
      thunders = th })
  end)
  if ok and sent then
    lastSig, lastSendT, modeSent = sig, now, mode
    if th then pending = {} end
  end
end

function M.status(now)
  now = now or clockhp()
  return { mode = mode, state = state.state, reason = state.reason, live = M.isLive(now),
    text = M.statusText(mode, state, now) }
end

return M

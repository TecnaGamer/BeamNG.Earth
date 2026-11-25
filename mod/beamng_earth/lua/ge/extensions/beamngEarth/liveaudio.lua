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

local mode = 'prerecorded'   -- the setting: 'prerecorded' | 'live'
local state = { state = 'off', t = nil }   -- the server's last audio_state and when it came (os.clockhp)
local lastSig, lastSendT, modeSent = nil, nil, nil
local fade = 0               -- 0: the mod's sounds full, 1: silent (live)
local fadeAt = nil
local pending = {}           -- thunders for the next message
local sender = nil           -- core.lua's: send(msg) -> true when sent (connected, a session)

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
  }
end

-- Pure: what decides a resend (rounded, so jitter does not send every frame).
function M.signature(m, f)
  return string.format('%s|%.2f|%.1f|%.2f|%s|%s|%.2f|%.2f|%s|%.2f', m, f.rainMmH, f.windMs, f.shelter,
    tostring(f.tunnel), tostring(f.cabin), f.volume, f.thunderVolume, tostring(f.muted), f.duck)
end

-- Pure: a thunder for the synth from storm.lua's strike s ({ t, seed }), its distance d m and storm.thunderFor(d) th:
-- id (the seed and time: a hint and the final one share it), at (UTC s the sound arrives), d, kind, gain, seed, final
-- (false: a look-ahead hint to render early; true: it struck, play it).
function M.thunder(s, d, th, final)
  local t, seed = tonumber(s.t) or 0, tonumber(s.seed) or 0
  return { id = string.format('%d@%.3f', seed, t), at = t + (tonumber(th.delay) or 0), d = tonumber(d) or 0,
    kind = th.kind, gain = tonumber(th.gain) or 0, seed = seed, final = final == true }
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
  state = { state = tostring(msg.state or 'off'), reason = msg.reason, latencyMs = tonumber(msg.latencyMs), t = clockhp() }
end

-- A new connection, its end or a level change: the server's word is gone, the pre-recorded sounds come back, and the
-- mode is said again.
function M.reset()
  state = { state = 'off', t = nil }
  lastSig, lastSendT, modeSent = nil, nil, nil
  pending = {}
end

function M.isLive(now) return M.liveNow(mode, state, now or clockhp()) end

-- The mod's own weather sounds' factor (1 full .. 0 silent), faded over FADE_S between the two.
function M.ownFactor(now)
  now = now or clockhp()
  local target = M.isLive(now) and 1 or 0
  local dt = fadeAt and math.max(0, math.min(now - fadeAt, 0.5)) or 0
  fadeAt = now
  local step = dt / M.FADE_S
  fade = fade < target and math.min(target, fade + step) or math.max(target, fade - step)
  return 1 - fade
end

-- The game's volumes: rain on the Other channel, thunder on the UI channel (the channels the mod's own play on).
function M.gameVolumes()
  local au = Engine and Engine.Audio
  local getChannel = au and au.getChannelVolume and function(c) return au.getChannelVolume(c, false) end or nil
  local s = rawget(_G, 'settings')
  local getSetting = s and s.getValue and function(k) return s.getValue(k) end or nil
  return M.channelVolume(getChannel, getSetting, 'AudioChannelOther', 'AudioOtherVol'),
    M.channelVolume(getChannel, getSetting, 'AudioChannelGui', 'AudioUiVol')
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

-- Every frame from ambience.update. ctx as M.fields' (the volumes and the quiet reasons filled here while live).
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
  local f = M.fields(ctx)
  local sig = M.signature(mode, f)
  if not M.shouldSend(mode, sig, lastSig, now, lastSendT, #pending) then return end
  local th = #pending > 0 and pending or nil
  local ok, sent = pcall(function()
    return send({ type = 'weather_audio', mode = mode, rainMmH = f.rainMmH, windMs = f.windMs,
      shelter = f.shelter, tunnel = f.tunnel, cabin = f.cabin, volume = f.volume, thunderVolume = f.thunderVolume,
      muted = f.muted, duck = f.duck, t = f.t, thunders = th })
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

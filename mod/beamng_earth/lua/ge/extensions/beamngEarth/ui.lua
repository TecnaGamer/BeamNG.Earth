-- The "BeamNG.Earth" imgui window (docs/V1_SPEC.md "Mod UI (v1)", docs/V1_1_SPEC.md). Engine-only module.
-- Plain strings go through TextUnformatted: Text/TextWrapped treat their argument as a
-- printf format (common/extensions/ui/imgui_luaintf.lua:291-310).
-- Once the pause-menu settings page exists (settings.lua, docs/DEVTOOLS.md) the window is a compact
-- status HUD (status line + quick buttons), hidden at level start unless the settings say otherwise;
-- "Full window" brings this window back for the session.

local i18n = require('beamngEarth/i18n')
local M = {}

M.visible = true
M.compact = false   -- set by settings.lua once the settings page exists
M.hudHidden = false -- the window starts hidden at level start (settings: hudVisible off)

local im = nil
local openPtr, latPtr, lonPtr, rebaseKmPtr = nil, nil, nil, nil

local function text(s) im.TextUnformatted(tostring(s)) end

local function wrapped(s)
  im.PushTextWrapPos(0)
  im.TextUnformatted(tostring(s))
  im.PopTextWrapPos()
end

local function fmtLatLon(lat, lon)
  if not lat then return 'n/a' end
  return string.format('%.6f, %.6f', lat, lon)
end

local function ms(v) return v and string.format('%.0f ms', v) or '-' end

-- The compact HUD: one status line, the far-field line and quick buttons.
local function drawCompact(view)
  im.SetNextWindowSize(im.ImVec2(380, 0), im.Cond_FirstUseEver)
  if im.Begin('BeamNG.Earth##hud', openPtr, im.WindowFlags_AlwaysAutoResize) then
    text(string.format('%s  |  anchor %s  |  %s', view.connection, view.anchorId and ('#' .. view.anchorId) or i18n.t('hud.window.none', nil, 'none'),
      view.dist and string.format('rebase in %.1f km', math.max(0, view.toRebase) / 1000) or '-'))
    if view.farText then text(view.farText) end
    wrapped('Status: ' .. (view.status ~= '' and view.status or '-'))
    local dev = rawget(_G, 'beamngEarth_dev')
    if dev then
      if im.Button(dev.isActive() and i18n.t('hud.window.devOff', nil, 'Dev tools off') or i18n.t('hud.window.dev', nil, 'Dev tools')) then dev.toggleLast() end
      im.SameLine()
    end
    if im.Button(i18n.t('hud.window.expanded', nil, 'Full window')) then M.compact = false end
    im.SameLine()
    text(i18n.t('hud.window.settings', nil, 'Settings: pause menu > BeamNG.Earth'))
  end
  im.End()
end

-- view: plain table of values to show; actions.teleport(lat, lon) is called on the button,
-- actions.setRebaseDistance(m) when the rebase slider is released.
function M.draw(view, actions)
  if not M.visible then return end
  im = im or ui_imgui
  if not im then return end
  if not openPtr then
    openPtr = im.BoolPtr(true)
    latPtr = im.DoublePtr(view.defaultLat)
    lonPtr = im.DoublePtr(view.defaultLon)
    rebaseKmPtr = im.FloatPtr((view.rebaseDistance or 10000) / 1000)
  end
  openPtr[0] = true
  if M.compact then
    drawCompact(view)
    if not openPtr[0] then M.visible = false end
    return
  end

  im.SetNextWindowSize(im.ImVec2(420, 0), im.Cond_FirstUseEver)
  if im.Begin('BeamNG.Earth', openPtr, im.WindowFlags_AlwaysAutoResize) then
    text('Server: ' .. view.connection)
    text('Anchor: ' .. (view.anchorId and ('#' .. view.anchorId .. '  ' .. fmtLatLon(view.anchorLat, view.anchorLon)) or i18n.t('hud.window.none', nil, 'none')))
    text('Player: ' .. fmtLatLon(view.playerLat, view.playerLon))
    if view.blockCx then
      text(string.format('Terrain block centre: (%d, %d)', view.blockCx, view.blockCy))
    end
    if view.toFollowPrepare then
      text(string.format('Terrain-follow: request in %.0f m, commit in %.0f m%s', math.max(0, view.toFollowPrepare),
        math.max(0, view.toFollowCommit), view.followPending and ('  - next ' .. view.followPending) or ''))
    end
    if view.dist then
      text(string.format('Distance from origin: %.0f m (rebase in %.0f m)%s', view.dist, math.max(0, view.toRebase),
        view.rebasePending and ('  - anchor ' .. view.rebasePending) or ''))
    end
    text(string.format('Last terrain-follow: %s (%d)   Last rebase: %s (%d)', ms(view.lastFollowMs), view.followCount or 0,
      ms(view.lastRebaseMs), view.rebaseCount or 0))
    if view.farText then text(view.farText) end
    local weather = rawget(_G, 'beamngEarth_weather') -- real weather (weather.lua), when loaded
    if weather and weather.statusLine then text(weather.statusLine()) end
    if rebaseKmPtr then
      im.SliderFloat(i18n.t('hud.window.originDistance', nil, 'Rebase distance (km)'), rebaseKmPtr, view.rebaseMin / 1000, view.rebaseMax / 1000, '%.1f km')
      if im.IsItemDeactivatedAfterEdit() and actions.setRebaseDistance then
        rebaseKmPtr[0] = actions.setRebaseDistance(rebaseKmPtr[0] * 1000) / 1000
      elseif not im.IsItemActive() then
        rebaseKmPtr[0] = view.rebaseDistance / 1000 -- follow console changes
      end
    end
    im.Separator()
    im.InputDouble(i18n.t('hud.window.latitude', nil, 'Latitude'), latPtr, 0, 0, '%.6f')
    im.InputDouble(i18n.t('hud.window.longitude', nil, 'Longitude'), lonPtr, 0, 0, '%.6f')
    if im.Button(i18n.t('hud.window.teleport', nil, 'Teleport')) then actions.teleport(latPtr[0], lonPtr[0]) end
    local dev = rawget(_G, 'beamngEarth_dev') -- dev tools (dev.lua): the last preset on / everything off
    if dev then
      im.SameLine()
      if im.Button(dev.isActive() and i18n.t('hud.window.devOff', nil, 'Dev tools off') or i18n.t('hud.window.dev', nil, 'Dev tools')) then dev.toggleLast() end
    end
    im.Separator()
    wrapped('Status: ' .. (view.status ~= '' and view.status or '-'))
    im.Separator()
    wrapped(view.attribution)
  end
  im.End()
  if not openPtr[0] then M.visible = false end
end

-- Values the window edits, for callers that need them (e.g. auto-teleport on level start).
function M.getLatLon(defaultLat, defaultLon)
  if latPtr then return latPtr[0], lonPtr[0] end
  return defaultLat, defaultLon
end

local latLonSet = false   -- a teleport set them this game run (else they are the defaults)
function M.setLatLon(lat, lon)
  if latPtr then latPtr[0], lonPtr[0] = lat, lon end
  latLonSet = true
end
function M.hasLatLon() return latLonSet and latPtr ~= nil end

return M

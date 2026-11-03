-- Development builds (the user, 2026-10-30: "add a disclaimers on git and in game that this version is based off the
-- development branch and has not been thoroughly tested"): the release workflow builds every push to the dev branch
-- as the GitHub pre-release v<VERSION>.dev.<n> (server updates.py). Its app tells the mod its version in the hello
-- (appVersion); a development build gets a notice at the top left until the player closes it (settings
-- devNoticeSeen: once per build), and the update notice speaks of development builds. Pure but for draw.

local M = {}

M.DISCORD = 'discord.gg/tgf'

-- Pure: is app version v a development build ("1.7.2-beta.dev.37")?
function M.isDev(v)
  return type(v) == 'string' and v:match('[%.%-]dev%.%d+$') ~= nil
end

-- Pure: the notice's lines for version v.
function M.lines(v)
  return {
    string.format('BeamNG.Earth %s is a development build.', tostring(v)),
    'It is built from the development branch and has not been thoroughly tested: expect bugs.',
    'Please report them on Discord (' .. M.DISCORD .. ') or GitHub. The beta releases are the tested ones.',
  }
end

-- Every frame in our level: the notice for a development build v (nil / a release: nothing). settings: the mod's
-- settings module; y: the window's top (px below the viewport's).
function M.draw(v, settings, y)
  if not M.isDev(v) or settings.get('devNoticeSeen') == v then return end
  local im = ui_imgui
  if not im then return end
  local vp = im.GetMainViewport()
  if vp then im.SetNextWindowPos(im.ImVec2(vp.Pos.x + 12, vp.Pos.y + (y or 12)), im.Cond_Always, im.ImVec2(0, 0)) end
  local flags = im.WindowFlags_AlwaysAutoResize + im.WindowFlags_NoCollapse + im.WindowFlags_NoSavedSettings
    + im.WindowFlags_NoTitleBar
  if im.Begin('BeamNG.Earth development build##beamngEarthDev', nil, flags) then
    local l = M.lines(v)
    im.TextColored(im.ImVec4(1.0, 0.75, 0.2, 1.0), l[1])
    im.Text(l[2])
    im.Text(l[3])
    if im.Button('Got it') then settings.set('devNoticeSeen', v) end
  end
  im.End()
end

return M

"""The sky's distance haze from above the atmosphere (the 3D map from orbit; the user, 2026-11-07: "fix the rendering
so i can zoom out as far as the apple map screenshots").

BeamNG.drive's sky (shaders/common/sky/RenderSkyRayMarching.h.hlsl, RenderCameraVolumePS: the camera volume, the
aerial perspective objects are hazed with) returns float4(0, 0, 0, 1) for a camera above the top of the atmosphere
along every ray that has not reached the atmosphere yet. Its alpha is the fog amount (skyFog.h.hlsl: transmittance =
1 - alpha), so every object seen from space was multiplied by 0: black (sky-high-altitude-facts: "camera at/above the
shell top draws objects BLACK"). No air on the way is no haze: float4(0, 0, 0, 0). Seen in game 2026-11-07: from
245 mi up with the normal 100 km atmosphere, black before, the ground lit after.

The game reads its sky shaders from its own folder, not through mods (a patched copy in our mod changed nothing), so
the launcher patches the game's file in place: the original kept first (backup_dir, by its hash), only the exact two
lines changed, nothing done to code we do not know (a game update's own file is patched again at the next start, or
left alone if it changed). restore() puts the original back. A flag file in our mod tells horizon.lua that the
atmosphere needs no raising any more.
"""
import hashlib
import os

SHADER = "shaders/common/sky/RenderSkyRayMarching.h.hlsl"
FLAG = "lua/ge/extensions/beamngEarth/skypatch_flag.lua"     # (the mod's horizon.lua: the shell stays as it is)
FLAG_TEXT = "-- written by the launcher's skypatch.py: the game's sky shader is patched\nreturn true\n"
FUNC = "float4 RenderCameraVolumePS(GeometryOutput Input)"
OLD = "return float4(0.0, 0.0, 0.0, 1.0);"
NEW = "return float4(0.0, 0.0, 0.0, 0.0); // BeamNG.Earth skypatch.py: no air on the way, no haze (alpha = fog)"
MARK = "BeamNG.Earth skypatch.py"
# the compute shader that includes it gets a marker line: the game's compiled-shader cache (temp/shaders/shaders.vk.db)
# is keyed by that file's own text, and with it unchanged the old build of the include was used (black again on a
# clean start, 2026-11-07)
MAIN = "shaders/common/sky/skyCameraVolume.hlsl"
MAIN_MARK = "// BeamNG.Earth skypatch.py: its include RenderSkyRayMarching.h.hlsl is patched (no haze from above the air)\n"
# copies an earlier version of this put in our mod (the game never read them): removed
OLD_MOD_FILES = (SHADER, "shaders/common/sky/skyCameraVolume.hlsl")


def patched(text):
    """The shader's text with the camera volume's two above-the-atmosphere returns fixed, or None when the function
    or those exact two lines are not there (another game version: left alone)."""
    i = text.find(FUNC)
    if i < 0:
        return None
    j = text.find("\n}", i)                 # (the function's closing brace at the line start)
    if j < 0:
        return None
    body = text[i:j]
    if body.count(OLD) != 2:
        return None
    return text[:i] + body.replace(OLD, NEW) + text[j:]


def unpatched(text):
    """The original text of a patched shader (the two lines back), or None when text is not ours."""
    return text.replace(NEW, OLD) if text.count(NEW) == 2 else None


def _write(path, text):
    tmp = path + ".bnge-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _flag(mod_dir, on):
    """Our mod's flag file on / off (never in the way: a failure leaves it as it is)."""
    flag = os.path.join(mod_dir, *FLAG.split("/"))
    try:
        if on:
            try:
                with open(flag, encoding="utf-8") as fh:
                    if fh.read() == FLAG_TEXT:
                        return
            except OSError:
                pass
            os.makedirs(os.path.dirname(flag), exist_ok=True)
            with open(flag, "w", encoding="utf-8") as fh:
                fh.write(FLAG_TEXT)
        elif os.path.isfile(flag):
            os.remove(flag)
    except OSError:
        pass


# The sky's own colour (skyLutSkyP.hlsl) took the camera as max(camPos.z, 50 m) straight over the origin: far from
# the origin on our curved world (the 3D map without moving the origin, the user 2026-11-07: "see if we can fix the
# grey rendering without haveing to rebase") the camera is km under z = 0, so the sky was drawn for 50 m over the
# origin, tilted against the ground and grey. Its place on the planet instead (the camera volume and the sky-view LUT
# already use the whole camera position), at least 50 m over the ground sphere: eyePosWorld (the same shader's fog
# uses it), camPos.xy did not move the horizon (seen in game 2026-11-07, 380 km from the origin).
SKY = "shaders/common/sky/skyLutSkyP.hlsl"
SKY_OLD = "const float3 worldPos = float3(0, 0, max(camPos.z, 50) * 1e-3) + float3(0, 0, Atmosphere.BottomRadius);"
SKY_NEW = ("float3 worldPos = eyePosWorld * 1e-3 + float3(0, 0, Atmosphere.BottomRadius); // " + MARK + ": the camera's own "
           "place on the planet\n    worldPos *= max(length(worldPos), Atmosphere.BottomRadius + 0.05) / "
           "max(length(worldPos), 1e-6);")


def _sky_file(game_dir, backup_dir, on):
    """skyLutSkyP.hlsl's camera line patched (on) or back: 'done', 'skipped' (not the code we know) or 'failed'."""
    path = os.path.join(game_dir, *SKY.split("/")) if game_dir else None
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except (OSError, TypeError, UnicodeDecodeError):
        return "skipped"
    if on:
        if SKY_NEW in text:
            return "done"
        if text.count(SKY_OLD) != 1:
            return "skipped"
        try:
            os.makedirs(backup_dir, exist_ok=True)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            with open(os.path.join(backup_dir, f"skyLutSkyP.{digest}.hlsl"), "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            _write(path, text.replace(SKY_OLD, SKY_NEW))
        except OSError:
            return "failed"
        return "patched"
    if SKY_NEW not in text:
        return "done"
    try:
        _write(path, text.replace(SKY_NEW, SKY_OLD))
    except OSError:
        return "failed"
    return "patched"


CACHE_FILES = ("temp/shaders/shaders.vk.db", "temp/shaders/pipelinecache.vk.db")


def refresh_cache(user_dir):
    """The game's compiled shaders set aside (renamed *.bnge-old, the previous such copy replaced), so it builds them
    again from the patched files: its cache kept the old sky (black from above the air until it was moved, 2026-11-07).
    The game's next start takes longer once."""
    if not user_dir:
        return
    for rel in CACHE_FILES:
        p = os.path.join(user_dir, *rel.split("/"))
        if os.path.isfile(p):
            try:
                os.replace(p, p + ".bnge-old")
            except OSError:
                pass


def apply(game_dir, mod_dir, backup_dir, user_dir=None):
    """Patch the game's shader in place (its original kept in backup_dir) and set our mod's flag: 'patched',
    'current' (patched already), 'skipped' (no game folder or file, or code we do not know: the flag removed) or
    'failed' (the game's folder not writable: the flag removed)."""
    for rel in OLD_MOD_FILES:
        p = os.path.join(mod_dir, *rel.split("/"))
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    src = os.path.join(game_dir, *SHADER.split("/")) if game_dir else None
    try:
        with open(src, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except (OSError, TypeError, UnicodeDecodeError):
        _flag(mod_dir, False)
        return "skipped"
    if unpatched(text) is not None:
        if _mark_main(game_dir, True) == "failed":
            return "failed"
        if _sky_file(game_dir, backup_dir, True) == "patched":
            refresh_cache(user_dir)
            _flag(mod_dir, True)
            return "patched"
        _flag(mod_dir, True)
        return "current"
    new = patched(text)
    if new is None:
        _flag(mod_dir, False)
        return "skipped"
    try:
        os.makedirs(backup_dir, exist_ok=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        with open(os.path.join(backup_dir, f"RenderSkyRayMarching.{digest}.hlsl"), "w", encoding="utf-8",
                  newline="") as fh:
            fh.write(text)
        _write(src, new)
    except OSError:
        _flag(mod_dir, False)
        return "failed"
    if _mark_main(game_dir, True) == "failed":
        return "failed"
    _sky_file(game_dir, backup_dir, True)
    refresh_cache(user_dir)
    _flag(mod_dir, True)
    return "patched"


def _mark_main(game_dir, on):
    """The marker line on (first line) / off in the game's compute shader: 'done' or 'failed'."""
    path = os.path.join(game_dir, *MAIN.split("/"))
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        has = text.startswith(MAIN_MARK)
        if on and not has:
            _write(path, MAIN_MARK + text)
        elif not on and has:
            _write(path, text[len(MAIN_MARK):])
    except OSError:
        return "failed"
    return "done"


def restore(game_dir, mod_dir=None, user_dir=None):
    """The game's original shader back (the two lines undone) and our flag removed: 'restored', 'original' (not
    patched) or 'failed'."""
    if mod_dir:
        _flag(mod_dir, False)
    sky = "done"
    if game_dir:
        _mark_main(game_dir, False)
        sky = _sky_file(game_dir, None, False)
    src = os.path.join(game_dir, *SHADER.split("/")) if game_dir else None
    try:
        with open(src, encoding="utf-8", newline="") as fh:
            text = fh.read()
        old = unpatched(text)
        if old is None:
            if sky == "patched":
                refresh_cache(user_dir)
                return "restored"
            return "original"
        _write(src, old)
    except (OSError, TypeError, UnicodeDecodeError):
        return "failed"
    refresh_cache(user_dir)
    return "restored"

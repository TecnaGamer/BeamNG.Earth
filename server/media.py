"""Desktop Spotify for the phone's Music app (the user, 2026-10-26: "can play my spotify"): the game's UI cannot play
Spotify, but the world server runs on the same PC, so it presses the media keys (play/pause, next, previous: Spotify
and every other player obey them) and reads the song from Spotify's window title ("Artist - Song" while it plays;
"Spotify" / "Spotify Premium" when paused). Windows only; elsewhere {"ok": False}.

The volume (the user, 2026-10-26: "a volume slider to the music app", Spotify only): Spotify's own level in the
Windows volume mixer (its audio sessions' ISimpleAudioVolume, through the Core Audio COM interfaces by ctypes: no
packages), so the game's sound is left as it is.
"""
import logging
import sys

log = logging.getLogger("earthd.media")

KEYS = {"play": 0xB3, "next": 0xB0, "prev": 0xB1}      # VK_MEDIA_PLAY_PAUSE, VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK
_last = {"title": "", "artist": ""}


def parse_title(title):
    """(playing, artist, song) of a Spotify window title."""
    t = (title or "").strip()
    if not t or t.lower().startswith("spotify"):
        return False, "", ""
    if " - " in t:
        artist, song = t.split(" - ", 1)
        return True, artist.strip(), song.strip()
    return True, "", t


def _spotify_title():
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0 or not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)       # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return True
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(520)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) and buf.value.lower().endswith("spotify.exe"):
                t = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, t, n + 1)
                found.append(t.value)
        finally:
            kernel32.CloseHandle(h)
        return True
    user32.EnumWindows(each, 0)
    return found[0] if found else None


# Core Audio (mmdeviceapi.h / audiopolicy.h): the GUIDs and the vtable slots used
CLSID_MMDeviceEnumerator = "BCDE0395-E52F-467C-8E3D-C4579291692E"
IID_IMMDeviceEnumerator = "A95664D2-9614-4F35-A746-DE8DB63617E6"
IID_IAudioSessionManager2 = "77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"
IID_IAudioSessionControl2 = "BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"
IID_ISimpleAudioVolume = "87CE5498-68D6-44E5-9215-6F24A0F8A1C9"
CLSCTX_ALL = 23
SLOT = {"QueryInterface": 0, "Release": 2, "EnumAudioEndpoints": 3, "Item": 4, "GetDefaultAudioEndpoint": 4, "Activate": 3, "GetSessionEnumerator": 5,
        "GetCount": 3, "GetSession": 4, "GetProcessId": 14, "SetMasterVolume": 3, "GetMasterVolume": 4}
SPOTIFY_EXE = "spotify.exe"


_warned = set()


def _warn_once(text):
    """A volume failure in the log once (the user's slider did nothing, 2026-10-26: the next time, the log says why)."""
    if text not in _warned:
        _warned.add(text)
        log.warning(text)


def clamp_volume(v):
    """0..100 (int) of any number, or None. Pure."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return int(round(min(100.0, max(0.0, v))))


def _spotify_volumes(fn):
    """fn(ISimpleAudioVolume pointer, vcall) for each of Spotify's audio sessions on every active output device (not
    only the default: Spotify may play through a headset; the user's slider found none, 2026-10-26); returns the
    results (empty: Spotify has no session, e.g. it has not played anything yet). The programs seen go to _seen."""
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("b", ctypes.c_ubyte * 16)]

    def guid(s):
        g = GUID()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(s).bytes_le, 16)
        return g

    def vcall(obj, name, argtypes, *args, restype=ctypes.HRESULT):
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[SLOT[name]])(obj, *args)

    def release(obj):
        if obj:
            vcall(obj, "Release", [], restype=ctypes.c_ulong)

    ole32, kernel32 = ctypes.windll.ole32, ctypes.windll.kernel32
    inited = ole32.CoInitializeEx(None, 0) >= 0      # (RPC_E_CHANGED_MODE: this thread is already set up otherwise)
    out, held = [], []
    try:
        enum = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(guid(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
                               ctypes.byref(guid(IID_IMMDeviceEnumerator)), ctypes.byref(enum))
        if not enum:
            return out
        held.append(enum)
        coll = ctypes.c_void_p()
        vcall(enum, "EnumAudioEndpoints", [ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)],
              0, 1, ctypes.byref(coll))                                        # eRender, DEVICE_STATE_ACTIVE
        held.append(coll)
        nd = wintypes.UINT()
        vcall(coll, "GetCount", [ctypes.POINTER(wintypes.UINT)], ctypes.byref(nd))
        _seen.clear()
        for di in range(nd.value):
            try:
                dev = ctypes.c_void_p()
                vcall(coll, "Item", [wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)], di, ctypes.byref(dev))
                held.append(dev)
                mgr = ctypes.c_void_p()
                vcall(dev, "Activate", [ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(guid(IID_IAudioSessionManager2)), CLSCTX_ALL, None, ctypes.byref(mgr))
                held.append(mgr)
                senum = ctypes.c_void_p()
                vcall(mgr, "GetSessionEnumerator", [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(senum))
                held.append(senum)
                n = ctypes.c_int()
                vcall(senum, "GetCount", [ctypes.POINTER(ctypes.c_int)], ctypes.byref(n))
            except OSError:         # (a device that will not say: the others still)
                continue
            for i in range(n.value):
                try:
                    got = _session(i, senum, vcall, GUID, guid, held, kernel32, fn)
                except OSError:         # (one odd session: the others still)
                    continue
                if got is not _SKIP:
                    out.append(got)
    finally:
        for o in reversed(held):
            try:
                release(o)
            except OSError:
                pass
        if inited:
            ole32.CoUninitialize()
    return out


_SKIP = object()
_seen = set()               # the programs with audio sessions at the last look (the log, when Spotify is not among them)


def _session(i, senum, vcall, GUID, guid, held, kernel32, fn):
    """fn's result for session i when it is Spotify's, else _SKIP."""
    import ctypes
    from ctypes import wintypes
    ctl = ctypes.c_void_p()
    vcall(senum, "GetSession", [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], i, ctypes.byref(ctl))
    held.append(ctl)
    ctl2 = ctypes.c_void_p()
    vcall(ctl, "QueryInterface", [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
          ctypes.byref(guid(IID_IAudioSessionControl2)), ctypes.byref(ctl2))
    held.append(ctl2)
    pid = wintypes.DWORD()
    try:
        vcall(ctl2, "GetProcessId", [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(pid))
    except OSError:         # (the system sounds session: no single process)
        return _SKIP
    exe = _exe_name(kernel32, pid.value)
    _seen.add(exe or "pid %d" % pid.value)
    if exe != SPOTIFY_EXE:
        return _SKIP
    vol = ctypes.c_void_p()
    vcall(ctl, "QueryInterface", [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
          ctypes.byref(guid(IID_ISimpleAudioVolume)), ctypes.byref(vol))
    held.append(vol)
    return fn(vol, vcall)


def _exe_name(kernel32, pid):
    import ctypes
    from ctypes import wintypes
    # (the handle a pointer: undeclared, ctypes cut it to a 32-bit int under 64-bit Python)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = kernel32.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(h)


def volume():
    """Spotify's mixer volume, 0..100, or None (not Windows, no Spotify session)."""
    if sys.platform != "win32":
        return None
    import ctypes

    def get(vol, vcall):
        f = ctypes.c_float()
        vcall(vol, "GetMasterVolume", [ctypes.POINTER(ctypes.c_float)], ctypes.byref(f))
        return f.value
    try:
        got = _spotify_volumes(get)
    except Exception as e:
        _warn_once("media volume: %s: %s" % (type(e).__name__, e))
        return None
    return clamp_volume(got[0] * 100) if got else None


def set_volume(v):
    """Spotify's mixer volume to v (0..100). True when a Spotify session took it."""
    v = clamp_volume(v)
    if v is None or sys.platform != "win32":
        return False
    import ctypes

    def put(vol, vcall):
        vcall(vol, "SetMasterVolume", [ctypes.c_float, ctypes.c_void_p], ctypes.c_float(v / 100.0), None)
        return True
    try:
        done = bool(_spotify_volumes(put))
        if not done:
            _warn_once("media set volume: Spotify has no audio session (it has not played since it started); "
                       "programs with sound: %s" % ", ".join(sorted(_seen)) if _seen else
                       "media set volume: no audio sessions found on any output device")
        return done
    except Exception as e:
        _warn_once("media set volume: %s: %s" % (type(e).__name__, e))
        return False


def state():
    """{"ok", "playing", "artist", "title", "volume"}: ok False when Spotify is not running (or not Windows); volume
    0..100 or None."""
    if sys.platform != "win32":
        return {"ok": False, "playing": False, "artist": "", "title": ""}
    try:
        title = _spotify_title()
    except Exception as e:      # an API change, no permission: no music info
        log.debug("media: %s: %s", type(e).__name__, e)
        title = None
    if title is None:
        return {"ok": False, "playing": False, "artist": "", "title": ""}
    playing, artist, song = parse_title(title)
    if playing:
        _last.update(title=song, artist=artist)
    out = {"ok": True, "playing": playing, "artist": _last["artist"], "title": _last["title"]}
    v = volume()
    if v is not None:
        out["volume"] = v
    return out


def press(action):
    """Presses the media key of action (play / next / prev). False when unknown or not Windows."""
    vk = KEYS.get(action)
    if vk is None or sys.platform != "win32":
        return False
    import ctypes
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)          # KEYEVENTF_KEYUP
    return True

"""Desktop Spotify for the phone's Music app (the user, 2026-10-26: "can play my spotify"): the game's UI cannot play
Spotify, but the world server runs on the same PC, so it presses the media keys (play/pause, next, previous: Spotify
and every other player obey them) and reads the song from Spotify's window title ("Artist - Song" while it plays;
"Spotify" / "Spotify Premium" when paused). Windows only; elsewhere {"ok": False}.
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


def state():
    """{"ok", "playing", "artist", "title"}: ok False when Spotify is not running (or not Windows)."""
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
    return {"ok": True, "playing": playing, "artist": _last["artist"], "title": _last["title"]}


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

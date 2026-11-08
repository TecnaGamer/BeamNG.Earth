"""The telemetry choice's defaults by region (the user, 2026-11-08: "do the middle ground but keep performance tick where
ok. but either way give all players a popup and let them choose before playing"; TELEMETRY.md, the research summary in
.claude/work/telemetry_perf_plan.md; not legal advice).

Read from this computer's own time zone, never asked of a server: before the player has chosen nothing may be sent.

    neutral  nothing ticked (consent: a pre-ticked box is none, CJEU Planet49): the EU-27 and EEA, China, Russia, India,
             and any time zone that cannot be placed
    perf_off basic statistics and network load ticked, performance data not: the UK (Children's Code), Canada (Quebec's
             Law 25), Brazil (ECA Digital), Turkey, South Korea
    all      everything ticked: everywhere else (the US, Japan, Australia, Switzerland, Singapore, South Africa, ...)

A time zone shared by countries of different groups takes the stricter (Windows' "GMT Standard Time" is London and
Lisbon: neutral). Windows' "Eastern Standard Time" is the US and Canada: the US's ("all").
"""
import os
import sys

NEUTRAL, PERF_OFF, ALL = "neutral", "perf_off", "all"
# Russia: no install id is kept (telemetry.py, a session's own random id)
NO_ID = {"ru"}

_EUROPE_NOT_EU = {
    "Europe/London": PERF_OFF, "Europe/Belfast": PERF_OFF, "Europe/Guernsey": PERF_OFF, "Europe/Isle_of_Man": PERF_OFF,
    "Europe/Jersey": PERF_OFF, "Europe/Zurich": ALL, "Europe/Istanbul": PERF_OFF,
}
_RUSSIA_ASIA = {"Yekaterinburg", "Omsk", "Novosibirsk", "Barnaul", "Tomsk", "Novokuznetsk", "Krasnoyarsk", "Irkutsk",
                "Chita", "Yakutsk", "Khandyga", "Vladivostok", "Ust-Nera", "Magadan", "Sakhalin", "Srednekolymsk",
                "Kamchatka", "Anadyr"}
_RUSSIA_EUROPE = {"Moscow", "Kaliningrad", "Samara", "Volgograd", "Saratov", "Ulyanovsk", "Astrakhan", "Kirov"}
_CHINA = {"Shanghai", "Urumqi", "Harbin", "Chongqing", "Chungking", "Kashgar"}
_BRAZIL = {"Sao_Paulo", "Bahia", "Belem", "Fortaleza", "Recife", "Maceio", "Araguaina", "Santarem", "Manaus", "Cuiaba",
           "Campo_Grande", "Porto_Velho", "Boa_Vista", "Rio_Branco", "Eirunepe", "Noronha"}
_CANADA = {"Toronto", "Montreal", "Vancouver", "Edmonton", "Winnipeg", "Halifax", "St_Johns", "Regina", "Moncton",
           "Glace_Bay", "Goose_Bay", "Iqaluit", "Whitehorse", "Dawson", "Yellowknife", "Inuvik", "Rankin_Inlet",
           "Resolute", "Cambridge_Bay", "Swift_Current", "Thunder_Bay", "Nipigon", "Rainy_River", "Atikokan", "Creston",
           "Dawson_Creek", "Fort_Nelson", "Blanc-Sablon", "Pangnirtung"}

# Windows' time zone keys (HKLM ... TimeZoneInformation\TimeZoneKeyName: English whatever the display language)
_WINDOWS = {
    **{k: NEUTRAL for k in (
        "W. Europe Standard Time", "Central Europe Standard Time", "Central European Standard Time",
        "Romance Standard Time", "E. Europe Standard Time", "FLE Standard Time", "GTB Standard Time",
        "GMT Standard Time", "Greenwich Standard Time", "Azores Standard Time", "Kaliningrad Standard Time",
        "Russian Standard Time", "Belarus Standard Time", "Astrakhan Standard Time", "Saratov Standard Time",
        "Volgograd Standard Time", "Russia Time Zone 3", "Ekaterinburg Standard Time", "Omsk Standard Time",
        "N. Central Asia Standard Time", "Altai Standard Time", "Tomsk Standard Time", "North Asia Standard Time",
        "North Asia East Standard Time", "Transbaikal Standard Time", "Yakutsk Standard Time",
        "Vladivostok Standard Time", "Magadan Standard Time", "Sakhalin Standard Time", "Russia Time Zone 10",
        "Russia Time Zone 11", "China Standard Time", "India Standard Time")},
    **{k: PERF_OFF for k in (
        "Korea Standard Time", "E. South America Standard Time", "Central Brazilian Standard Time",
        "Tocantins Standard Time", "Bahia Standard Time", "SA Eastern Standard Time", "Turkey Standard Time",
        "Newfoundland Standard Time", "Atlantic Standard Time", "Canada Central Standard Time")},
}


def for_zone(name):
    """The group of an IANA time zone name ("Europe/Berlin") or a Windows key ("W. Europe Standard Time")."""
    if not name:
        return NEUTRAL
    if name in _WINDOWS:
        return _WINDOWS[name]
    if " " in name:                 # (another Windows key: the Americas, Asia-Pacific, Africa)
        return ALL
    if name in _EUROPE_NOT_EU:
        return _EUROPE_NOT_EU[name]
    area, _, city = name.partition("/")
    if area == "Europe":
        return NEUTRAL              # (the EU, the EEA; Russia's European zones; the rest of Europe: the stricter)
    if area == "Atlantic" and city in ("Azores", "Canary", "Madeira", "Reykjavik", "Faroe", "Faeroe"):
        return NEUTRAL
    if area == "Asia" and city in _RUSSIA_ASIA | _CHINA | {"Kolkata", "Calcutta", "Nicosia", "Famagusta"}:
        return NEUTRAL
    if name in ("PRC", "Asia/Hong_Kong", "Asia/Macau") or name.startswith("W-SU"):
        return NEUTRAL
    if name in ("Asia/Seoul", "ROK", "Asia/Istanbul", "Turkey", "GB", "GB-Eire") or name.startswith("Canada/") \
            or name.startswith("Brazil/"):
        return PERF_OFF
    if area == "America" and (city in _BRAZIL or city in _CANADA):
        return PERF_OFF
    if area in ("America", "US", "Asia", "Australia", "Pacific", "Africa", "Indian", "Atlantic", "Antarctica") \
            or name in ("UTC", "Etc/UTC") or name.startswith("Etc/GMT"):
        return NEUTRAL if name in ("UTC", "Etc/UTC") or name.startswith("Etc/") else ALL
    return NEUTRAL


def is_russia(name):
    if not name:
        return False
    area, _, city = name.partition("/")
    return (area == "Europe" and city in _RUSSIA_EUROPE) or (area == "Asia" and city in _RUSSIA_ASIA) \
        or name.startswith("W-SU") or name in ("Russian Standard Time", "Kaliningrad Standard Time",
                                               "Ekaterinburg Standard Time", "Russia Time Zone 3",
                                               "Russia Time Zone 10", "Russia Time Zone 11")


def local_zone():
    """This computer's time zone name (IANA on Linux / the Steam Deck / macOS, the registry key on Windows), or ""."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and "/" in tz and not tz.startswith("/"):
        return tz
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as k:
                return str(winreg.QueryValueEx(k, "TimeZoneKeyName")[0]).strip("\x00 ")
        except OSError:
            return ""
    for path in ("/etc/localtime",):
        try:
            real = os.path.realpath(path)
            if "zoneinfo/" in real:
                return real.split("zoneinfo/", 1)[1]
        except OSError:
            pass
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def region():
    """(group, russia) of this computer."""
    z = local_zone()
    return for_zone(z), is_russia(z)

"""Replay past storms through the snow kernel (winter W1, native snow.rs) against observed snow depth.

For each storm: Open-Meteo's historical forecast (the same hourly variables the server fetches, server/weather.py
SNOW_HOURLY: the forecast models' own archive from 2022) at the station, and the station's daily observed snow depth
(NOAA GHCN-Daily SNWD, through NCEI's access service). The kernel runs over the whole window from the first day's model
depth (as the server seeds it); each day's depth at the observation hour (~12 UTC: US co-op morning readings are
local 7 am) is compared with the station's. Responses are kept in --cache (re-runs need no network).

    python3 tools/snowreplay.py --cache ~/bnge-tests/winter/w1/replay [--storm NAME] [--days]

Prints per storm: the peak depth (model / kernel / observed), the depth error at the observed peak, and the days until
bare ground; --days prints each day. Targets (PLAN.md 4.5, a guess): depth within 30 % or 3 cm at the storm's end,
bare ground within 2 days.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import fast  # noqa: E402
import snow  # noqa: E402
import weather  # noqa: E402

UA = "BeamNG.Earth snow replay (https://github.com/TecnaGamer/BeamNG.Earth)"
HF_API = "https://historical-forecast-api.open-meteo.com/v1/forecast"
GHCN_API = "https://www.ncei.noaa.gov/access/services/data/v1"
# (name, GHCN station, lat, lon, first day, last day, Koppen-based regime is looked up; what happened)
STORMS = (
    ("minneapolis-2023-02", "USW00014922", 44.885, -93.231, "2023-02-17", "2023-03-08",
     "Feb 21-23 2023: ~15 in on a 4 in pack"),
    ("denver-2024-03", "USW00003017", 39.833, -104.658, "2024-03-10", "2024-03-24",
     "Mar 13-15 2024: a wet spring storm, fast melt"),
    ("buffalo-2022-12", "USW00014733", 42.941, -78.736, "2022-12-20", "2023-01-04",
     "Dec 23-25 2022 blizzard, then a thaw"),
    ("burlington-2023-03", "USW00014742", 44.468, -73.150, "2023-03-11", "2023-03-26",
     "Mar 14-15 2023 nor'easter"),
    ("chicago-2024-01", "USW00094846", 41.960, -87.932, "2024-01-08", "2024-01-26",
     "Jan 9-13 2024: several storms and an arctic outbreak"),
)


def get_json(url, path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    time.sleep(1.0)
    return data


def forcing(storm, cache):
    name, _st, lat, lon, d0, d1, _ = storm
    q = {"latitude": lat, "longitude": lon, "start_date": d0, "end_date": d1, "hourly": ",".join(weather.SNOW_HOURLY),
         "wind_speed_unit": "ms", "timeformat": "unixtime", "timezone": "auto", "cell_selection": "land"}
    return get_json(HF_API + "?" + urllib.parse.urlencode(q, safe=","), os.path.join(cache, f"{name}_hf.json"))


def observed(storm, cache):
    name, st, _lat, _lon, d0, d1, _ = storm
    q = {"dataset": "daily-summaries", "stations": st, "startDate": d0, "endDate": d1,
         "dataTypes": "SNWD,SNOW,PRCP,TMAX,TMIN", "format": "json", "units": "metric", "includeStationLocation": 1}
    rows = get_json(GHCN_API + "?" + urllib.parse.urlencode(q), os.path.join(cache, f"{name}_obs.json"))
    out = {}
    for r in rows:
        if r.get("SNWD") not in (None, ""):
            out[r["DATE"]] = float(r["SNWD"])
    elev = float(rows[0]["ELEVATION"]) if rows and rows[0].get("ELEVATION") else None
    return out, elev


def replay(storm, cache, obs_hour=12):
    fast.require(snow.NATIVE, "snow replay")
    om = forcing(storm, cache)
    obs, elev = observed(storm, cache)
    recs, times, z_cell, _off = snow.records(om)
    z_site = int(round(elev)) if elev is not None else z_cell
    regime = snow.regime_at(storm[2], storm[3])
    model_depth = {t: v for t, v in zip(times, om["hourly"].get("snow_depth") or [])}
    days, prev = [], -1
    for i, t in enumerate(times):
        if time.gmtime(t).tm_hour != obs_hour:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        m = snow.message(recs, i, t, storm[2], storm[3], z_site, z_cell, regime, prev)
        prev = m["ground"][1]
        md = model_depth.get(t)
        days.append({"day": day, "kernel": m["ground"][0], "model": None if md is None else round(md * 1000, 1),
                     "obs": obs.get(day), "p3": m["roads"][3][0], "p0": m["roads"][0][0]})
    return days, {"z_site": z_site, "z_cell": z_cell, "regime": regime}


def summary(days):
    obs = [d for d in days if d["obs"] is not None]
    if not obs:
        return None
    peak = max(obs, key=lambda d: d["obs"])
    kpeak = max(d["kernel"] for d in days)
    mpeak = max((d["model"] or 0) for d in days)

    def bare(key):          # the first day after the observed peak with < 1 cm
        after = False
        for d in days:
            if d["day"] == peak["day"]:
                after = True
            v = d[key]
            if after and v is not None and v < 10:
                return d["day"]
        return None
    err = peak["kernel"] - peak["obs"]
    ok = abs(err) <= max(30.0, 0.3 * peak["obs"])
    return {"obs_peak": peak["obs"], "obs_peak_day": peak["day"], "kernel_at_peak": peak["kernel"],
            "kernel_peak": kpeak, "model_peak": mpeak, "error_mm": round(err, 1), "within": ok,
            "bare_obs": bare("obs"), "bare_kernel": bare("kernel")}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", default=os.path.expanduser("~/bnge-tests/winter/w1/replay"))
    ap.add_argument("--storm")
    ap.add_argument("--days", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    out = {}
    for s in STORMS:
        if a.storm and a.storm != s[0]:
            continue
        days, site = replay(s, a.cache)
        sm = summary(days)
        out[s[0]] = {"site": site, "summary": sm, "days": days, "what": s[6]}
        print(f"{s[0]} ({s[6]}; station {site['z_site']} m, model cell {site['z_cell']} m, regime M{site['regime']})")
        if a.days:
            for d in days:
                print(f"  {d['day']}  obs {d['obs']!s:>6} mm  kernel {d['kernel']:6.1f} mm  model {d['model']!s:>6} mm"
                      f"  P0 {d['p0']:<9} P3 {d['p3']}")
        if sm:
            print(f"  peak obs {sm['obs_peak']:.0f} mm on {sm['obs_peak_day']}: kernel {sm['kernel_at_peak']:.0f} mm "
                  f"(error {sm['error_mm']:+.0f} mm, {'within' if sm['within'] else 'OUTSIDE'} 30 %/3 cm); kernel peak "
                  f"{sm['kernel_peak']:.0f}, model peak {sm['model_peak']:.0f}; bare ground obs {sm['bare_obs']}, "
                  f"kernel {sm['bare_kernel']}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()

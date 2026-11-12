# Telemetry: what BeamNG.Earth collects, and why

BeamNG.Earth sends small anonymous reports so we can see how many people play, keep the free map services it uses
from being overloaded, and find what makes the game slow or crash on real players' machines. This page lists every
field, why we want it, and how you choose what is sent.

**Nothing in a report says who you are or where you are in the world.** No name, account, e-mail, IP address,
coordinates, place names, routes or map tiles are ever sent or stored.

## Your choice, before anything is sent

**Nothing is sent until you have chosen.** The first time you start BeamNG.Earth (and again whenever we want to send a
new kind of data) the welcome screen asks, with three switches:

| switch | what it covers |
|---|---|
| **Usage statistics** | the main switch: the basic player statistics (section 1). Off: **nothing at all** is sent, whatever the two switches under it say. |
| ↳ **Performance data** | the performance, world-generation, system and error data (sections 2-5) |
| ↳ **Network load** | the per-map-service request counts (section 6) |

How the boxes start depends on the privacy rules where you are. BeamNG.Earth decides that **on your own computer,
from its time zone setting** (on Windows also its region setting, because Windows names the US's and Canada's time
zones alike; the stricter of the two is used). It asks no server, and neither setting is sent anywhere.

| where (by time zone) | boxes start |
|---|---|
| the EU, Iceland, Liechtenstein, Norway, the rest of Europe, China, Russia, India, UTC or a time zone that cannot be placed | **nothing ticked**: you tick what you want to send |
| the UK, Canada, Brazil, Turkey, South Korea | usage statistics and network load ticked, **performance data not** |
| everywhere else (for example the US, Japan, Australia, Switzerland) | all ticked |

Wherever you are, you see the switches and choose before playing, and you can change them any time in BeamNG.Earth
settings (Interface). The switches are read before every report, so a change applies from the next one. Telemetry is
also off when the app runs with `--offline`, with the environment variable `BNE_TELEMETRY=off`, and it never runs
during automated tests.

In Russia no install id is kept: each session uses a new random one, so sessions cannot be linked to each other.

## How it is sent

Every 5 minutes the BeamNG.Earth app (the companion server on your computer, not the game) sends one HTTPS report to
`earth.tgforever.net/telemetry`, a server run by the BeamNG.Earth project. A report that cannot be sent is dropped;
nothing is queued up on disk. A report is about 2 KB; with everything on, that is roughly 30 KB an hour.

## 1. Basic player statistics (main switch)

| field | example | why |
|---|---|---|
| install id | 32 random hex characters | to count players online without knowing who they are. It is made from random numbers when the app first runs (file `telemetry_id` in the cache folder); it contains nothing about you or your computer. Delete the file and you get a new one. |
| app version | `1.8.2-beta` | how many players are on each version, whether an update reached people |
| operating system | `windows` / `linux` / `deck` / `macos` | which platforms to test on |
| game running | yes / no | players actually in the game vs app open |
| BeamMP | yes / no | how many play multiplayer |

**Country**: the collector looks up the country from the connection's IP address **and then throws the address
away**. Countries are shown publicly only when at least 5 players are online from them.

## 2. System and settings (Performance data)

Sent once per session, and again only when something changes.

| field | example | why |
|---|---|---|
| CPU model, cores, threads | `AMD Ryzen 5 3600`, 6, 12 | which processors struggle with world generation |
| GPU model, VRAM (rounded) | `NVIDIA GeForce RTX 3060`, 12 GB | which graphics cards hitch or run out of memory |
| system RAM (rounded) | 32 GB | memory limits: the server's caches are sized from it |
| operating system, window system | linux, wayland | driver and platform problems |
| screen class and refresh (rounded) | 1440p, 144 Hz | performance depends heavily on resolution |
| graphics API | DX11 / DX12 / Vulkan | driver-specific problems |
| game version | `0.40.5` | engine changes between updates |
| game graphics settings | preset, mesh, textures, terrain, lighting, shadows, anisotropic, AA, vsync, depth of field, SSAO, motion blur, reflections, grass, display mode | to compare like with like |
| BeamNG.Earth settings | quality preset, trees and their distance, ground cover, grass distance, rocks, night lights, distant traffic, view distance, terrain detail, building distance, ground photo size, object detail, roads, clouds, sky mode, map style, frame pacing, full-priority loading | which of our options cost the most |
| other mods (rounded) | 0, 1, 2-4, 5-9, ... | other mods can slow the game too |

Amounts are rounded into steps, so they describe a class of machine rather than one machine. Only the settings named
here are read; nothing else from your game folder.

## 3. Scene complexity, instead of location (Performance data)

We never send where you are. To know how demanding the scene was, a report says what kind of place it was:

| field | example | why |
|---|---|---|
| scene | seconds of the 5 minutes by how many buildings were loaded around you: none / rural / town / urban / dense | a city costs far more than a field |
| what was loaded (rounded) | road tiles, buildings, far buildings, trees, far tiles, traffic cars, parked cars, lit lamps | what the game had to draw |
| how you moved | seconds stopped and in speed bands (under 30, 30-60, 60-100, 100-150, over 150 mph), seconds with the 3D map open | high speed is where world generation falls behind |
| teleports | how many | each teleport loads a new place |
| session length | how long the game had been running, as one of: under 30 min / 30-60 min / 1-2 h / 2-4 h / over 4 h | to see whether long sessions get slower |
| time since a teleport | seconds and hitches in steps: under 10 s / 10-30 s / 30 s-2 min / later | whether the stutter comes with loading a new place |
| idle | seconds and hitches while the car has not moved and the camera has not changed for 2 minutes (away from the keyboard) | a game left running is not how it plays |

## 4. Performance and world-generation speed (Performance data)

Over each 5 minutes:

| field | example | why |
|---|---|---|
| frame times | how many frames fell into fixed steps (gives average fps, 1% low, 95th / 99th percentile); frames while the game is paused or in the background are left out | how smooth the game was |
| this window's own lows | the median, 99th and 99.9th percentile frame time of the 5 minutes (one number each, rounded to 0.25 ms) and how many frames took over 2x and 4x the median | a true 1% low per window, so one slow machine does not set everyone's figure |
| the same per speed band | the steps for town speeds vs the highway | so fast driving is not hidden in an average |
| hitches | how many frames over 50 / 100 / 250 / 1000 ms (and how many of those while loading after a teleport, and per speed band), and for the slow ones the name of the part that took longest (a BeamNG.Earth part such as `roads`, or an engine subsystem such as `RenderShadows`, or "outside" both), the frames over 1 s counted apart | which part to fix first |
| what was going on in slow frames | counts by fixed names only: ground block swap, collision rebuild, far tiles imported or retextured, roads built, distant traffic, trees placed or new tree shapes, server messages handled, a vehicle spawned or removed, the catch-up boost, the map or a menu open, the world origin moving, a Lua garbage sweep, loading after a teleport, the car held for the ground | which of our jobs makes the stutter |
| fps drops | how many, how long, the lowest (rounded) | stutter you notice |
| memory (rounded) | the game's Lua memory and its garbage per minute; the game's own memory (the most in the 5 minutes), the computer's free memory (the least), the graphics memory in use (the most); the BeamNG.Earth server's memory, its limit (the Max RAM for BeamNG.Earth setting: Auto or the amount chosen, rounded) and its memory tier, and how many worker processes it runs | running out of memory on 16 GB machines and the Steam Deck; long sessions filling the graphics memory |
| terrain blocks | how long each new ground block was waited for, in steps | whether the world keeps up with you |
| keeping up | seconds held at the edge of the ground, seconds held until the ground loads, seconds loading after a teleport, how many times and how long the server borrowed extra CPU cores | the "world can't keep up" problem |
| road tiles | how many were waiting (rounded) | roads appearing late |
| server load | CPU cores the server used (rounded) | how hard BeamNG.Earth works your PC |

## 5. Error fingerprints (Performance data)

| field | example | why |
|---|---|---|
| server errors | a short code made from the error's type and the line of our code it came from (`a3f9c2e1`), how many times, and the error's type and number (`OSError:28`, "disk full") | which bugs players actually hit, so we fix those first; the number tells a full disk from a file in use |

A fingerprint is a hash: it contains **no error message text, no file paths, no user names and no list of your
other mods**. It only lets us count how often the same bug happens.

Everything in sections 2-5 is checked by the app before it can leave your computer: only the fields named on this page,
only numbers (and in section 2 short model names and settings values), so no position, path or free text can slip in.

## 6. Network load (Network load switch)

| field | example | why |
|---|---|---|
| requests per map service | `basemap.nationalmap.gov`: 88 requests, 2.8 MB, 0 errors, 0 rate-limited | BeamNG.Earth downloads from free public services (map data, elevation, photos). These counts show us whether we are putting too much load on any of them, so we can cache or slow down before a service blocks everyone. |

## What happens to the data

- **Sole purpose**: counting players, protecting the free map services BeamNG.Earth relies on, and finding what to
  fix or speed up. The data is never sold, shared for advertising, or used to profile anyone.
- The collector adds each report to **totals**: daily histograms per version, GPU model, CPU model, settings preset,
  area type and speed band. **Individual reports are not kept.** The install id is used only to count who is online
  and is never stored next to the performance numbers.
- A breakdown (for example one GPU model) is shown only when at least **5 installs** are behind it; rarer hardware
  is grouped as "other".
- **Public pages** (earth.tgforever.net): players online, versions, systems and countries (`/players`), and a
  **performance summary** (`/performance`): fps, smoothness and world-generation speed by version, GPU, settings preset
  and area type, from these totals only.
- **Kept**: player counts as 1-minute samples for 7 days, 5-minute samples for 90 days, hourly peaks indefinitely; the
  performance totals per day. None of these can be traced back to one install.
- **Where**: the collector is a server run by the BeamNG.Earth project in the United States.
- **Your install id**: the file `telemetry_id` in the BeamNG.Earth cache folder. Delete it and you get a new id; with
  the main switch off, no id is sent at all.

## Who runs it

BeamNG.Earth is an open-source project by TecnaGamer (github.com/TecnaGamer/BeamNG.Earth). The code that builds and
sends reports is in `server/telemetry.py` and the collector in `native/serverlist/`: anyone can check that it does
what this page says.

## A note for parents

BeamNG.Earth does not ask for anyone's age, name or contact details, and nothing in a report identifies a person. If
you would rather your child sends nothing, untick **Usage statistics** on the welcome screen or in settings.

## We ask again

If we ever want to collect a new kind of data, it is added here first and the welcome screen asks everyone again
before any of it is sent.

## Status

- **From the release with the welcome-screen choice**: sections 1 and 6 (basic statistics and network load), only after
  your choice. Releases before it sent sections 1 and 6 by default, with an off switch in settings.
- **Development builds** (the `dev.N` pre-releases): sections 2-5 (performance data) are sent from 2026-11-08 on,
  only while Performance data is ticked. The welcome screen asks everyone again first (also those who chose before).
- **Releases**: from the release after 1.8.2, sections 2-5 are sent as on the development builds, only while
  Performance data is ticked. (Earlier releases measured them and kept them next to the last report without sending.)
- **From the release after 1.9.1** (and the development builds before it): the window's own lows, what was going
  on in slow frames, time since a teleport, idle time, the game's, the system's and the graphics memory and the server's
  worker count (consent version 3: the welcome screen asks everyone again first).
- **Being added**: a **What is sent** button that shows the last report exactly as it left your computer, and the
  public performance page.

Questions or concerns: open an issue on GitHub or ask on Discord.

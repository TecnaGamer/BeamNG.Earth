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
from its time zone setting**; it asks no server, and the time zone is not sent anywhere.

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
| cache drive | SSD / HDD | slow disks make loading slow |
| screen resolution and refresh (rounded) | 1440p, 144 Hz | performance depends heavily on resolution |
| graphics API | DX11 / DX12 / Vulkan | driver-specific problems |
| game graphics settings | preset, mesh, textures, terrain, lighting, shadows, AA, vsync, fps limit | to compare like with like |
| BeamNG.Earth settings | quality preset, terrain detail, tree and building distance, ground photo size, view to horizon, traffic, weather / sky mode, map layers on | which of our options cost the most |
| mod and app versions match | yes / no, both versions | on a BeamMP server the server's copy of the mod replaces yours; a mismatch makes loading slow |

Amounts like RAM, VRAM and resolution are rounded into steps, so they describe a class of machine rather than one
machine.

## 3. Scene complexity, instead of location (Performance data)

We never send where you are. To know how demanding the scene was, a report says what kind of place it was:

| field | example | why |
|---|---|---|
| area type | share of the 5 minutes in dense city / town / suburban / rural / wilderness / over water | a city costs far more than a field |
| what was loaded (rounded) | road tiles, buildings, trees, far tiles, traffic and parked cars, lamps | what the game had to draw |
| terrain type | flat / rolling / mountainous | hills change world-generation work |
| how you moved | seconds stopped and in speed bands (under 30, 30-60, 60-100, 100-150, over 150 mph), flying, with the 2D or 3D map open | high speed is where world generation falls behind |
| teleports | how many; first visit or a place you had been | first visits download data, revisits use the cache |

## 4. Performance and world-generation speed (Performance data)

Over each 5 minutes:

| field | example | why |
|---|---|---|
| frame times | a histogram in fixed steps (gives average fps, 1% low, 95th / 99th percentile) | how smooth the game was |
| the same per speed band | fps and 1% low in town vs on the highway | so fast driving is not hidden in an average |
| hitches | how many frames over 50 / 100 / 250 / 1000 ms, and what caused each (Lua, garbage collection, physics, rendering, shadows, the GPU, BeamNG.Earth's own parts) | which part to fix first |
| fps drops | how many, how long, the lowest | stutter you notice |
| memory (rounded) | game, Lua, BeamNG.Earth server | running out of memory on 16 GB machines and the Steam Deck |
| teleport to playable | seconds, first visit and revisit separately | how long you wait after choosing a place |
| terrain blocks | how many, how many were ready in time, how long the others took | whether the world keeps up with you |
| keeping up | seconds with the edge warning, times and seconds the car was held, time the server needed extra CPU cores | the "world can't keep up" problem |
| road tiles | how far ahead they were, how many waited | roads appearing late |
| caches | how often the cache had what was needed | download vs disk |
| server load | CPU cores used, memory peak, worker restarts | how hard BeamNG.Earth works your PC |

## 5. Crash and error fingerprints (Performance data)

| field | example | why |
|---|---|---|
| server errors | a short code made from the error's type and the line of our code it came from (`a3f9c2e1`), and how many times | which bugs players actually hit, so we fix those first |
| mod (Lua) errors | the same kind of code | the same, for the game side |
| crashed last time | yes / no | crashes we otherwise never hear about |

A fingerprint is a hash: it contains **no error message text, no file paths, no user names and no list of your
other mods**. It only lets us count how often the same bug happens.

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
- **Being added**: sections 2-5 (performance data), a **What is sent** button that shows the last report exactly as it
  left your computer, and the public performance page. They start with the release whose notes say so; this page is
  updated with it.

Questions or concerns: open an issue on GitHub or ask on Discord.

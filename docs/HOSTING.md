# Hosting: BeamNG.Earth's CDN and backend

This page explains how BeamNG.Earth's own data hosting and backend services are set up: where each file comes from,
who builds it, how it is cached, and what it costs. It is written for two groups of readers:

- people who want to **run their own CDN** (or a private mirror for a group of players), and
- people who want to **contribute to, review or suggest changes to** the backend.

You do not need any of this to play. The game works without the project's CDN: every dataset on it has a fallback
to the original open sources, so the CDN only makes the world load faster and takes load off those sources.

Related pages: [SOURCES.md](../SOURCES.md) (every host the client talks to, as players see it),
[TELEMETRY.md](../TELEMETRY.md) (what the collector receives and keeps), [hosting/](../hosting/README.md) (the
deploy script, service wrapper and unit files).

## Contents

1. [Architecture](#architecture)
2. [Bucket and domain](#bucket-and-domain)
3. [The Cloudflare cache rule](#the-cloudflare-cache-rule)
4. [The switch file (cdn.json)](#the-switch-file-cdnjson)
5. [Datasets on the CDN](#datasets-on-the-cdn)
6. [The data repository and its workflows](#the-data-repository-and-its-workflows)
7. [The collector service](#the-collector-service)
8. [The on-demand aerial builder](#the-on-demand-aerial-builder)
9. [Signed decision archives](#signed-decision-archives)
10. [Client settings (environment variables)](#client-settings-environment-variables)
11. [Run your own CDN, step by step](#run-your-own-cdn-step-by-step)
12. [Costs](#costs)
13. [Contributing and suggesting changes](#contributing-and-suggesting-changes)

## Architecture

```
  GitHub: TecnaGamer/BeamNG.Earth-data (public)
  +-----------------------------------------------------------------+
  | cdn.json - the switch file: which CDN datasets players use      |
  | GitHub Actions                                                  |
  |   data (weekly)  bnwt (monthly)  tiles / dem / aerial (by hand) |
  |   build jobs: no secrets  ->  upload jobs: checker + aws-cli    |
  | GitHub Pages: dataset manifests (and a fallback copy of files)  |
  +-------------------------------+---------------------------------+
                                  | S3 API uploads (R2 token in repo secrets)
                                  v
  +-------------------------------+   custom domain    +-------------------------------+
  | Cloudflare R2 bucket          | -----------------> | tiles.tgforever.net           |
  | bnge-tiles                    |                    | Cloudflare edge cache         |
  |  s2wc/  dem/  aerial/  bnwt2/ |                    | (cache rule "R2 tiles";       |
  |  dec/   data/ osmtags/        |                    |  /bnwt* goes straight to R2)  |
  +-------------------------------+                    +---------------+---------------+
                ^                                                      |
                | S3 API uploads                                       | HTTPS GET, range reads
                |                                                      v
  +-------------+---------------------------+          +-------------------------------+
  | small always-on Linux box               |          | player's PC                   |
  |  collector (native/serverlist, Rust)    | <------- |  BeamNG.Earth server (server/)|
  |   behind a TLS reverse proxy:           |  HTTPS   |  - reads the CDN first        |
  |   earth.tgforever.net                   |          |  - falls back to the original |
  |   /servers /telemetry /report           |          |    open sources               |
  |   /aerial/request -> spool folder       |          |  - asks for aerial builds     |
  |  aerial builder (holds bucket creds)    |          |    (anonymous, opt-in)        |
  |  decision builder (signing key; planned)|          +-------------------------------+
  +-----------------------------------------+
```

The main ideas:

- **Static files only.** The CDN is a bucket of immutable files behind Cloudflare's cache. There is no application
  server in front of the data; the player's own BeamNG.Earth server reads the files.
- **A fallback for everything.** If a file is missing, the CDN is down or a dataset is switched off, the client uses
  the original open source (Sentinel-2 from EOX, Mapterhorn's tile endpoint, Overture's files, and so on).
- **Builds run in public.** Most data is built by GitHub Actions in the public data repository, so anyone can read
  the exact code and inputs. In the aerial and BNWT workflows, the jobs that build run without secrets; only small,
  simple upload jobs hold the bucket token.
- **Only the project's machines write.** Nothing a player's machine makes is ever served to others. Players can only
  *ask* for a build (an aerial cell), and the request contains nothing about who asked.
- **A kill switch.** R2 bills per request and has no spending cap, so one file in the data repo (`cdn.json`) turns
  each dataset on or off for every player within about an hour.

## Bucket and domain

| | |
|---|---|
| Storage | a Cloudflare R2 bucket, `bnge-tiles` |
| Public address | the custom domain `https://tiles.tgforever.net`, connected to the bucket in R2's settings (*Settings > Custom Domains*) |
| Writes | the S3-compatible API at `https://<account id>.r2.cloudflarestorage.com`, with `aws-cli` |

Why R2: it has no egress (download) fees, and with a custom domain Cloudflare's edge cache answers most reads, so
most player traffic costs nothing. Do not use the bucket's `r2.dev` address for players: it is rate-limited and meant
for testing only.

**API tokens.** Create them under *R2 > Manage API tokens*:

- **Object Read & Write**, scoped to this one bucket. This is enough for the aerial, tiles, dem and data uploads.
- The **bnwt** workflow also lists the bucket (`aws s3 ls`) to find the shards that are already done, so its token
  must also be allowed to **list** objects. (The tiles and dem workflows list too, to resume.)
- Give each machine or repository its own token, so one can be revoked without breaking the others.

Uploads use `aws-cli`. With aws-cli 2.23 or newer, set `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` and
`AWS_RESPONSE_CHECKSUM_VALIDATION=when_required`; otherwise R2 refuses multipart uploads with `InvalidPart`.

## The Cloudflare cache rule

One Cache Rule on the zone, named **"R2 tiles"** and placed first:

- **When:** host is `tiles.tgforever.net`, the path does **not** end with `.json`, and the path does **not** start
  with `/bnwt`:

  ```
  (http.host eq "tiles.tgforever.net" and not ends_with(http.request.uri.path, ".json")
   and not starts_with(http.request.uri.path, "/bnwt"))
  ```

- **Then:**
  - Cache eligibility: **Eligible for cache**.
  - Edge TTL: **use the cache-control header if present, bypass the cache if not**. So an object is cached at the
    edge only when its upload set a `Cache-Control` header (see the table in [Datasets](#datasets-on-the-cdn)).
  - Status code TTL: **404 cached for 1 day** (most 404s are "no pack here, open sea" and never change).
  - Browser TTL: **respect origin**.

**Why `.json` is excluded:** JSON files on the CDN are small, changing index files (aerial coverage, BNWT indexes,
dataset manifests, shard records). They must reach players quickly after a build, so they always come from R2.

**Why `/bnwt` is excluded:** BNWT parts are files of up to 400 MB that the client reads with HTTP range requests.
On a cache miss, Cloudflare first pulls the **whole file** from R2 before it answers the range: one range read of a
400 MB part took **8.9 s** on a miss, against about **0.3 s** straight from R2 (a cache hit took 0.13 s). Most BNWT
reads are first visits to a place, so most would be misses. If the parts are ever cut to about 32 MB, caching them
could be turned back on.

**Size limit:** Cloudflare's free plan caches files up to 512 MB. That is why every large archive on the CDN
(BNWT, the OSM road tags) is cut into parts of at most 400 MB.

**Testing the cache:** use a `GET`, not a `HEAD`. A `HEAD` request (`curl -I`) can report `cf-cache-status: DYNAMIC`
even for a cached file.

```sh
curl -s -o /dev/null -D - https://tiles.tgforever.net/s2wc/v1/9/32/22.bngp | grep -i -E "cf-cache-status|cache-control"
# cf-cache-status: HIT once the file is in that edge's cache (the first read there is a MISS)
```

## The switch file (cdn.json)

`cdn.json` lives at the root of the data repository and is read raw from GitHub
(`https://raw.githubusercontent.com/TecnaGamer/BeamNG.Earth-data/main/cdn.json`; `server/cdn.py`). The client
reads it at start and at most once an hour, and keeps the last copy on disk. If it cannot be read, or a dataset is
not listed, that dataset is **off** and the client uses the original sources. An example with every key (the data
repo holds the current file):

```json
{"imagery":   {"bngs2":  {"enabled": true, "max_zoom": 13, "max_lat": {"14": 50}},
               "bngaer": {"enabled": true, "request": true}},
 "dem":       {"bngmt":  {"enabled": true, "release": "0.0.13"}},
 "decisions": {"enabled": true, "request": false}}
```

| key | turns on | options |
|---|---|---|
| `imagery.bngs2` | Sentinel-2 packs for the distant land | `max_zoom` (12 = the JPEG packs; 13 or 14 = the WebP packs), `max_lat` (where a zoom's packs end) |
| `imagery.bngaer` | the 1 m aerial packs | `request: false` = never ask for on-demand builds |
| `dem.bngmt` | the Mapterhorn z0-12 elevation packs | `release`: which Mapterhorn release's packs to read |
| `decisions` | the signed decision archives | `request: true` = ask for missing blocks (only when the collector supports it) |

BNWT is not in the switch file yet: it is on only when a player sets `BNGE_BNWT_URL`.

Changing `cdn.json` reaches every player within about an hour, without a release. Treat an edit like a release.

## Datasets on the CDN

All paths are relative to the bucket root, which is also `https://tiles.tgforever.net/`.

### Overview

| path | what | built by | how often | `Cache-Control` | edge cached |
|---|---|---|---|---|---|
| `s2wc/v1/{z}/{px}/{py}.bngp` | Sentinel-2 photo packs | data repo `tiles` | by hand | `public, max-age=2592000, immutable` (30 days) | yes |
| `dem/mapterhorn/<release>/{z}/{px}/{py}.bngp` | Mapterhorn elevation, z0-12 | data repo `dem` | by hand, per Mapterhorn release | `public, max-age=2592000, immutable` | yes |
| `dem/mapterhorn/<release>/index.bin` | which packs exist | data repo `dem` | once per release | `public, max-age=86400` | yes |
| `aerial/v1/g<gen>/{z}/{px}/{py}.bngp` | 1 m aerial packs | data repo `aerial`; the builder box | by hand; on demand | `public, max-age=31536000, immutable` (1 year) | yes |
| `aerial/v1/cov/<rx>/<ry>.json` | aerial coverage per region | the same | after every build | `public, max-age=300` | no (`.json`) |
| `aerial/v1/_cells/<x>_<y>.json` | per-cell build records | the same | after every cell | `no-cache` | no |
| `bnwt<FORMAT>/<release>/...pmtiles` | prebaked world input tiles | data repo `bnwt` | monthly | `public, max-age=31536000, immutable` | no (`/bnwt`) |
| `bnwt<FORMAT>/<release>/*.json` | BNWT indexes, manifests, shard records | data repo `bnwt` | monthly | `no-cache` | no |
| `dec/d<FORMAT>/<vkey>/...` | signed decision archives | the builder box | after each release | see [below](#signed-decision-archives) | see below |
| `data/<dataset>/<key>/...` | mirror of the weekly datasets | data repo `data` (`tools/mirror_data.py`) | weekly | `public, max-age=31536000, immutable` | yes |
| `data/<dataset>/<key>/manifest.json` | marks a complete copy | the same | weekly | `public, max-age=3600` | no (`.json`) |
| `osmtags/<key>/osmtags*.pmtiles` | OpenStreetMap road tags | data repo `data` (job `osmtags`) | weekly | `public, max-age=31536000, immutable` | yes |

A **pack** (`.bngp`, `server/tilepack.py`) holds the 8 x 8 tiles of one zoom under a tile three zooms up, so one
request fetches 64 tiles. A pack that does not exist (a 404) means "no tiles here" (open sea, or nothing built);
the client remembers a 404 from the tile host for 30 days, so upload a dataset completely before switching it on.

Keys like `<release>`, `<gen>` and `<key>` make the data files **immutable**: a rebuild writes new paths instead of
changing old files. That is what makes the long cache times safe. The exceptions are the small files that say what
exists (coverage files, indexes, manifests, records), which change in place and get short lifetimes or none, and the
decision blocks (see below).

### Sentinel-2 photo packs: `s2wc/v1/`

- **What:** ESA WorldCover's 2021 Sentinel-2 composite (CC BY 4.0), tone-mapped into Web Mercator tiles: JPEG for
  zooms 3 to 12, WebP for 13 and 14 (zoom 14 only to 50 degrees latitude). The client uses them for the distant land,
  ahead of USGS and EOX.
- **Built by:** `tools/build_s2_packs.py`, run by the data repo's `tiles` workflow, one job per zoom-5 region. Zoom 13
  and up is resumable: a finished region leaves a marker under `s2wc/v1/_done/<zoom>/`.
- **How often:** by hand. The source composite does not change.
- **Size:** the zoom-13 WebP packs of all land are about 83 GB.

### Mapterhorn elevation: `dem/mapterhorn/<release>/`

- **What:** [Mapterhorn](https://mapterhorn.com/attribution)'s `planet.pmtiles`, zooms 0 to 12, repacked into packs
  with the tile bytes **unchanged**. Mapterhorn allows storing and redistributing its tiles with attribution, and its
  maintainer asked apps to use the archives instead of the tile endpoint. The client reads zooms 0 to 12 here and asks
  Mapterhorn's endpoint only for the deeper zooms.
- **Files:** the packs, `index.bin` (which packs exist; written last, and the client uses a release only once it is
  there) and `release.json`.
- **Built by:** `tools/build_mt_packs.py` in the data repo's `dem` workflow. It reads the archive with range reads
  (never stores it whole), checks the archive's ETag on every read, and checks its MD5 before writing `index.bin`.
- **How often:** by hand, for each new Mapterhorn release.
- **Size:** about the size of the source archive (331 GiB for release 0.0.13).

### 1 m aerial packs: `aerial/v1/`

- **What:** AVIF 4:4:4 tiles of zooms 14 to 17 (about 1 m per pixel at zoom 17), only from photos whose licence
  allows handing out derived tiles: USDA NAIP (public domain) through Microsoft's Planetary Computer, and the open
  European national orthophotos. Never commercial imagery.
- **Files:** `g<gen>/{z}/{px}/{py}.bngp` (a rebuild is a new generation), and one coverage file per zoom-6 region,
  `cov/<rx>/<ry>.json`, saying which zoom-11 cells (about 15 km across) hold which packs. The coverage files are
  rebuilt from the per-cell records in `_cells/`. The format is in `tools/build_aerial_packs.py`.
- **Built by:** `tools/build_aerial_packs.py`, in two places:
  - the data repo's `aerial` workflow builds the **prebaked set** (metro areas and main road corridors);
  - the [on-demand builder](#the-on-demand-aerial-builder) builds other cells when players ask for them.
- **Size:** a zoom-17 pack is about 0.3 to 0.7 MB; a cell is up to 64 + 21 packs.

### Prebaked world input tiles (BNWT): `bnwt<FORMAT>/<release>/`

- **What:** for each zoom-14 cell, exactly what the client's own Overture Maps reader would produce (`server/bnwt.py`),
  in a lossless format, so a first visit needs a few range reads instead of many large Parquet reads. Four archives:
  `base` (water, land use, infrastructure), `transportation`, `buildings` and `places`. `<release>` is the Overture
  release, `<FORMAT>` the tile codec version (currently `bnwt2`).
- **Files:** per archive, `<name>.c<CONTENT>.index.json` (written **last**: a client only reads an archive through
  its index), a manifest with sizes and SHA-256s, the parts (PMTiles v3, at most 400 MB each), the attribution files,
  and `_build/` records of the finished shards.
- **Built by:** `tools/build_bnwt.py`, from a **pinned** BeamNG.Earth release tag or commit, in the data repo's
  `bnwt` workflow: thousands of shards spread over many jobs, resumable.
- **How often:** monthly (the 26th, after Overture's release), plus by hand.
- **Size:** roughly 200 GB per world release (estimate). Not edge-cached, so every read is an R2 read.
- **Status:** players use it only with `BNGE_BNWT_URL=https://tiles.tgforever.net`.

### Signed decision archives: `dec/d<FORMAT>/<vkey>/`

Pre-computed road plans and cell facts for popular places, for exactly one server version. See
[Signed decision archives](#signed-decision-archives) for the layout, the signing and the recommended headers.

### Weekly data mirror: `data/` and `osmtags/`

- **What:** the weekly datasets the data repo publishes on GitHub Pages (railways, lane tags, highway signs, fuel
  and charging stations, US level crossings, measured roofs and others; see the data repo's README) are also copied
  to the CDN by `tools/mirror_data.py` in the data repo. Each dataset's `manifest.json` on Pages names that copy in its
  `"data"` field; the client reads the files there and falls back to Pages. Reason: players read about 1.15 TB a
  month of these files, far over GitHub Pages' soft limit of 100 GB.
- **Layout:** `data/<dataset>/<key>/<the same paths as on Pages>`, where `<key>` is the build time plus a hash of the
  files. A dataset that did not change is not uploaded again. `manifest.json` is uploaded last and marks a complete
  copy.
- **OSM road tags:** `osmtags/<key>/osmtags-NNN.pmtiles`, OpenStreetMap's road tags (lanes, widths, speed limits,
  surfaces...) from OSM US's Layercake file, in parts of at most 400 MB (about 2.4 GB per build). Only its manifest is
  on Pages.
- **How often:** weekly, Fridays 03:17 UTC (a new OpenStreetMap planet appears on Thursdays).
- **Housekeeping:** old `osmtags/<key>/` and `data/` keys are not pruned automatically yet.

## The data repository and its workflows

Most data is built by GitHub Actions in the public repository
**[TecnaGamer/BeamNG.Earth-data](https://github.com/TecnaGamer/BeamNG.Earth-data)**. Its README describes every
dataset; this section explains how the workflows fit together. Read the workflows themselves for the details: each
starts with a comment explaining its inputs.

| workflow | builds | schedule | uploads to |
|---|---|---|---|
| `rail.yml` (named "data") | railways, lane tags, highway signs, stations, US level crossings, OSM road tags; the Pages site | weekly, Fridays 03:17 UTC | GitHub Pages, `data/`, `osmtags/` |
| `tiles.yml` | Sentinel-2 packs | by hand | `s2wc/v1/` |
| `dem.yml` | Mapterhorn packs | by hand | `dem/mapterhorn/<release>/` |
| `aerial.yml` + `aerial-chunk.yml` | the prebaked aerial set | by hand | `aerial/v1/` |
| `bnwt.yml` + `bnwt-shards.yml` | BNWT archives | monthly (26th), or by hand | `bnwt<FORMAT>/<release>/` |

The repository also holds `cdn.json` (the switch file) and `addons.json` (the in-game add-ons list), which players
read raw from GitHub.

### Security design

The bucket token can overwrite what every player downloads, so the workflows are built to keep it away from
complex code:

- **Build jobs hold no secrets.** In the `aerial` and `bnwt` workflows, the jobs that run the builders (Python,
  Pillow, GDAL, DuckDB, the native module, downloaded photos and map data) have no access to the token. They hand
  their output on as workflow artifacts.
- **Upload jobs run almost nothing.** The jobs that hold the token run only a small standard-library checker from the
  data repo itself (`tools/aerial_ci.py`, `tools/bnwt_ci.py`) and `aws-cli`. The checker rejects the whole artifact
  if any file is not what its record says: a pack's header against its path, a part's size and SHA-256 against its
  shard's record.
- **Pinned code.** `bnwt` runs BeamNG.Earth's `tools/build_bnwt.py` at one `code_ref` for the whole run: a release
  tag or a full commit SHA, never a branch (by default the latest published release, never a dev pre-release).
  `aerial` uses a pinned copy of the builder that lives in the data repo.
- **Pinned actions.** Third-party actions in these workflows are pinned by full commit SHA, not by tag.
- **Least privilege.** Workflow permissions are `contents: read`; the R2 token is scoped to the one bucket.
- **Ordered uploads.** Data first, then the record or index that makes it visible, so a client never sees a
  half-uploaded build.

The older `rail.yml`, `tiles.yml` and `dem.yml` workflows run only code that lives in the data repo itself (copies of
the builders, never code fetched from elsewhere in a job with the token), but they still build and upload in the same
job and pin actions by version tag. Moving them to the split design is welcome work.

### Secrets

Set these in the data repo's *Settings > Secrets and variables > Actions*:

| secret | what |
|---|---|
| `R2_ACCOUNT_ID` | the Cloudflare account ID (the workflows build the endpoint `https://<id>.r2.cloudflarestorage.com` from it) |
| `R2_ACCESS_KEY_ID` | the R2 API token's access key ID |
| `R2_SECRET_ACCESS_KEY` | the R2 API token's secret access key |

The token needs **Object Read & Write** on the bucket, plus permission to **list** objects for `bnwt` (and for the
resumable `tiles` and `dem` runs). Never print these values in a workflow; `aws-cli` reads them from the environment.

## The collector service

The collector is a small Rust program, [`native/serverlist`](../native/serverlist), running on the project's own
small always-on Linux box behind a reverse proxy with TLS, at `https://earth.tgforever.net`. It handles:

| endpoint | what |
|---|---|
| `POST /announce`, `GET /servers` | the BeamMP server list shown in the game's BeamNG.Earth tab. A server can only list itself: the address is the one the request comes from. Kept in memory only. |
| `POST /telemetry` | the anonymous usage and performance reports ([TELEMETRY.md](../TELEMETRY.md)), added to totals; individual reports are not kept |
| `GET /players`, `/performance`, `/stats...` | the public statistics pages, built from those totals |
| `POST /report` | player reports sent from the in-game reporter. **Private** and **rate-limited** (TELEMETRY.md, "Reports you send") |
| `POST /aerial/request`, `GET /aerial/status` | the front door for [on-demand aerial builds](#the-on-demand-aerial-builder) |

The collector never holds bucket credentials. It looks up a country for each connection from DB-IP's free table and
then drops the address; addresses are used only in memory, for rate limits.

To run one, see [hosting/README.md](../hosting/README.md): build it, deploy it with `hosting/deploy_serverlist.sh`,
and put a TLS reverse proxy in front.

## The on-demand aerial builder

The prebaked aerial set covers metro areas and main roads. Everywhere else in the areas with open photos, the first
player who drives there can ask for the place to be built, so the next visit (theirs or anyone's) loads faster:

1. The client checks the region's coverage file. If the cell is not built, it sends
   `POST /aerial/request {"v": 1, "cell": [x, y]}` (only with the player's usage statistics switched on; at most
   once per cell every 6 hours per machine and at most 6 cells an hour). Meanwhile, the live photo sources draw the place.
2. The collector checks the cell is inside an area with open photos, removes duplicates, applies its limits (12 new
   cells an hour and 40 a day per address, 600 a day in all, 3,000 waiting) and writes an empty file
   `queue/<x>_<y>` into a spool folder.
3. The builder service, on the same box, takes the oldest cell, builds it with `tools/build_aerial_packs.py`, uploads
   the packs, then the cell's record, then the region's new coverage file, and moves the entry to `done/` (or `none/`
   when there are no open photos there).
4. The client reads the coverage file again (every 10 minutes, sooner while it waits for a build) and switches to
   the packs.

The builder is a pinned copy of a few files from one commit, wrapped by
[`hosting/aerial-builder/run.py`](../hosting/aerial-builder/run.py), which adds the credentials mapping, a disk guard
and crash recovery. It is the only process on the box that holds the bucket credentials (`r2.env`, mode 600; names
in [`hosting/r2.env.example`](../hosting/r2.env.example)). A systemd path unit starts it only once that file exists.
Setup steps: [hosting/README.md](../hosting/README.md).

## Signed decision archives

**What they are.** On a first visit, the client spends much of its time planning roads (grading, junctions) and
computing per-cell facts. These results depend only on the server version and the map data, so the project's builder
can compute them once for popular places and publish them (`server/decisions.py`, `tools/build_decisions.py`,
`native/earthfast/src/decisions.rs`). The client downloads a block instead of planning it, and plans it itself on any
miss, version mismatch, bad signature or network error.

**Layout** (under `BNGE_DECISIONS_URL`, by default `https://tiles.tgforever.net/dec`):

```
d<FORMAT>/<vkey>/manifest.bngm      signed: the version key's document and the list of blocks
d<FORMAT>/<vkey>/12/<x>/<y>.bngd    one zoom-12 block (4 x 4 cells, about 8 km across), signed
```

`<vkey>` is a SHA-256 of everything that changes the output besides the place: the plan and fact formats, the build
switches and the map data versions. Every release has a new `<vkey>`, so a player only ever matches archives built
for exactly their version.

**Why signed.** These archives replace computation on the player's machine, so a client must be sure the project
made them. Each block and manifest carries an ed25519 signature, checked whole before anything in it is used. Only
the project's builder makes them; nothing a player's machine produced is ever served.

**The key.** The signing key is created on the builder box and **never leaves it**. Its public key is compiled into
the client (`TRUSTED` in `native/earthfast/src/decisions.rs`):

```
33255e3e9e9182e0edcf1d528f347b520ae6fd17e7eb9f145349fc2b97aee57e
```

**Make your own key** (if you build your own archives):

```sh
# with the repo's native module built: writes a 32-byte seed (hex) with mode 600, prints the public key (hex)
python3 tools/build_decisions.py make-test-key ~/bnge-builder/decisions-signing.key

# or with OpenSSL: the seed and the public key as hex
openssl genpkey -algorithm ed25519 -out key.pem
openssl pkey -in key.pem -outform DER | tail -c 32 | xxd -p -c 32 > decisions-signing.key   # the seed
openssl pkey -in key.pem -pubout -outform DER | tail -c 32 | xxd -p -c 32                   # the public key
chmod 600 decisions-signing.key key.pem
```

Then either set `BNGE_DECISIONS_PUBKEY=<public key hex>` on the clients (a comma-separated list; it **replaces** the
compiled keys), or put your key into `TRUSTED` and build the native module yourself. Never commit or share the seed.

**Build and upload:**

```sh
python3 tools/build_decisions.py build --places "@top:200" --cache ~/bnge-builder/decisions-cache \
    --out ~/bnge-builder/dec-out --key ~/bnge-builder/decisions-signing.key --procs 4
# upload the <vkey> folder: the blocks first, the manifest last
aws s3 cp ~/bnge-builder/dec-out/d1/<vkey> s3://<bucket>/dec/d1/<vkey> --recursive --exclude manifest.bngm \
    --endpoint-url "$R2_ENDPOINT" --content-type application/octet-stream --cache-control "public, max-age=3600"
aws s3 cp ~/bnge-builder/dec-out/d1/<vkey>/manifest.bngm s3://<bucket>/dec/d1/<vkey>/manifest.bngm \
    --endpoint-url "$R2_ENDPOINT" --content-type application/octet-stream --cache-control "no-cache"
```

The builder needs a full server checkout at the release commit (with its native module), its own cache (10 to 15 GB)
and many CPU hours: planning is 15 to 90 s per block, and `@top:200` at the default 6 km squares is about 7,200 km2.
The output is about 60 to 200 KB per km2 (0.4 to 1.5 GB per version). Run it at low priority, like the aerial
builder. It downloads map data from the same open sources a player would, so keep it within their rate limits.

**Cache headers.** A block may be rebuilt in place (merged with new places and signed again), and the client reads
the manifest again every 10 minutes, so the manifest should not be edge-cached (`no-cache`) and blocks should have a
short lifetime (for example one hour). Uploading is a manual step today. The collector does not yet have an endpoint
for requesting missing blocks (TELEMETRY.md, section 8), so keep `"request": false` in the switch file.

## Client settings (environment variables)

These are read by the BeamNG.Earth server on the player's PC (`server/`). Set them in the environment that starts the
launcher or `run_server.sh` / `run_server.bat`. With the launcher you can also pass them to the server alone as
`BNGE_SERVER_ENV="NAME=value NAME2=value2"`.

### The CDN

| variable | default | what it does |
|---|---|---|
| `BNGE_CDN_BASE` | `https://tiles.tgforever.net` | the tile host: Sentinel-2 packs (`/s2wc/v1`), Mapterhorn packs (`/dem/mapterhorn`), and the default base of the aerial packs and decision archives. The client's rate-limit policy for the CDN follows this host. |
| `BNGE_CDN_SWITCH` | the data repo's `cdn.json` (raw GitHub URL) | where the [switch file](#the-switch-file-cdnjson) is read from |
| `BNGE_AERIAL_BASE` | `<BNGE_CDN_BASE>/aerial/v1` | the aerial packs and coverage files |
| `BNGE_AERIAL` | the switch file | `on` / `off`: use the aerial packs whatever the switch says |
| `BNGE_AERIAL_REQUEST_URL` | `https://earth.tgforever.net/aerial/request` | where to ask for on-demand aerial builds; `off` never asks |
| `BNGE_DEM_CDN` | the switch file | a Mapterhorn release to read from the CDN (e.g. `0.0.13`), or `off` |
| `BNGE_S2_MAX_ZOOM`, `BNGE_S2_MAX_LAT` | the switch file | the Sentinel-2 packs' finest zoom (12 to 14) and per-zoom latitude limits (`14:50`); normally set from the switch |
| `BNGE_BNWT_URL` | unset (off) | where the BNWT archives are: `https://...`, `file://...` or a folder. Set it to `https://tiles.tgforever.net` to use the project's. |
| `BNGE_DECISIONS` | the switch file | `on` / `off`: use the decision archives whatever the switch says |
| `BNGE_DECISIONS_URL` | `<BNGE_CDN_BASE>/dec` | where the decision archives are |
| `BNGE_DECISIONS_PUBKEY` | the compiled `TRUSTED` keys | ed25519 public keys (hex, comma-separated) to accept **instead of** the compiled ones |
| `BNGE_DECISIONS_REQUEST`, `BNGE_DECISIONS_REQUEST_URL` | off; `https://earth.tgforever.net/decisions/request` | ask for missing decision blocks (not deployed yet) |

### Other hosted data and services

| variable | default | what it does |
|---|---|---|
| `BNE_DATA_PAGES` | `https://tecnagamer.github.io/BeamNG.Earth-data` | the weekly datasets' manifests (each names its CDN copy in `"data"`) |
| `BNE_OSMTAGS_BASE` | `<BNE_DATA_PAGES>/osmtags` | the OSM road tags manifest's folder (a staged build) |
| `BNE_NYC_MASSING_URL`, `BNE_NYC_TANKS_URL` | the data repo's Pages | New York building massing and rooftop tanks |
| `BNE_TELEMETRY_URL` | `https://earth.tgforever.net/telemetry` | where anonymous telemetry goes (`BNE_TELEMETRY=off` sends nothing) |
| `BNGE_REPORT_URL` | `https://earth.tgforever.net/report` | where in-game reports go |

The BeamMP server list address is fixed in `server/earthd.py` (`SERVER_LIST_URL`). The collector's own settings are
listed in [hosting/README.md](../hosting/README.md).

## Run your own CDN, step by step

You can mirror any subset of the datasets. Whatever you leave out falls back to the original sources (or to the
project's CDN, if you keep `BNGE_CDN_BASE` pointing there and override only some bases).

### 1. Create the bucket and domain

1. In Cloudflare, create an R2 bucket.
2. Connect a custom domain to it (*R2 > your bucket > Settings > Custom Domains*), for example
   `tiles.example.org`. The domain's zone must be on Cloudflare.
3. Create an R2 API token: **Object Read & Write**, this bucket only, plus **list** if you will run `bnwt`. Note the
   access key ID, the secret and your account ID; store them only in a password manager and in the places below.
4. Add the [cache rule](#the-cloudflare-cache-rule), with your host name.

Any S3-compatible storage with an HTTPS front that supports range requests also works; R2 is what the project uses
because it has no egress fees.

### 2. Point a build at your bucket

**With GitHub Actions (recommended):**

1. Fork [TecnaGamer/BeamNG.Earth-data](https://github.com/TecnaGamer/BeamNG.Earth-data).
2. In the fork, replace the bucket name `bnge-tiles` and the host `tiles.tgforever.net` with yours. They appear in
   the workflows (`BUCKET:` and the `s3://` paths) and in `tools/mirror_data.py` (`BUCKET`, `PUBLIC`).
3. Add the three secrets (`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`) to the fork.
4. Run the workflows you want from the *Actions* tab. Start with a dry run where the workflow offers one
   (`aerial`, `bnwt`), or a small region (`tiles` with one `x,y`, `dem` with `only`). Large builds are resumable: run
   the workflow again with the same inputs and it continues.
5. Edit `cdn.json` in your fork to switch on what you have built.

**By hand (a small area, or a private group):** run the builders locally and upload the output folder. For example,
the Sentinel-2 packs of one zoom-5 region:

```sh
python3 tools/build_s2_packs.py out/s2wc --zoom 12 --z5 16 10
aws s3 cp out/s2wc s3://<bucket>/s2wc/v1 --recursive --endpoint-url "https://<account id>.r2.cloudflarestorage.com" \
    --content-type application/octet-stream --cache-control "public, max-age=2592000, immutable"
```

Use the same paths and `Cache-Control` headers as in the [table above](#overview). Upload data before the files that
point at it (packs before coverage files, parts before indexes, blocks before manifests).

For a quick local test there is no need for a bucket at all: `BNGE_BNWT_URL` takes a plain folder, and
`tools/aerial_mock.py` serves aerial packs and the request endpoint on `127.0.0.1`.

### 3. Point a client at your base

On each player's PC (or in your launcher script):

```sh
export BNGE_CDN_BASE=https://tiles.example.org              # Sentinel-2, Mapterhorn, and the default for the next two
export BNGE_CDN_SWITCH=https://raw.githubusercontent.com/<you>/BeamNG.Earth-data/main/cdn.json
export BNGE_BNWT_URL=https://tiles.example.org              # only if you built BNWT
export BNE_DATA_PAGES=https://<you>.github.io/BeamNG.Earth-data   # only if your fork publishes the weekly data
# optional: your own collector
export BNGE_AERIAL_REQUEST_URL=https://collector.example.org/aerial/request    # or "off"
export BNE_TELEMETRY_URL=https://collector.example.org/telemetry
export BNGE_REPORT_URL=https://collector.example.org/report
# only if you sign your own decision archives
export BNGE_DECISIONS_PUBKEY=<your public key hex>
```

On Windows, use `set NAME=value` in the window that starts the launcher, or `BNGE_SERVER_ENV`.

Check that it works: at start, the server's log names each CDN dataset it turned on and the base it reads from (lines
starting with `cdn:`).

### 4. Optional: run the services

If you want on-demand aerial builds or your own server list and statistics, set up the
[collector](#the-collector-service) and the [aerial builder](#the-on-demand-aerial-builder) on a small Linux box,
following [hosting/README.md](../hosting/README.md).

## Costs

R2's list prices at the time of writing (check [Cloudflare's pricing page](https://developers.cloudflare.com/r2/pricing/)
for current ones):

| | price | free each month |
|---|---|---|
| storage | $0.015 per GB-month | 10 GB-month |
| Class A operations (writes, lists) | $4.50 per million | 1 million |
| Class B operations (reads) | $0.36 per million | 10 million |
| egress (downloads) | free | |

What that means for this setup:

- **Storage is the main fixed cost.** Rough sizes: Sentinel-2 zoom 13 about 83 GB, Mapterhorn z0-12 about 330 GiB,
  BNWT about 200 GB per release (estimate), OSM road tags about 2.4 GB per weekly build, aerial packs growing with
  coverage. Prune old releases and weekly keys you no longer serve.
- **Edge cache hits cost nothing.** Only cache misses reach R2 as Class B reads. Packs (64 tiles per request) keep
  the request count low, and the long `Cache-Control` times keep the hit rate high.
- **BNWT reads are never cached**, so each range read is a Class B operation. Watch these if you turn BNWT on for
  many players.
- **Uploads are Class A operations.** Many small files cost more than a few big ones; that is another reason for
  packs.
- **There is no spending cap on R2.** Watch the bucket's metrics in the dashboard, and use the switch file to turn a
  dataset off if its request count runs away.

The collector and builder run on one small box; GitHub Actions minutes are free for public repositories.

## Contributing and suggesting changes

- **Client side** (how the server reads the CDN, fallbacks, rate limits): this repository. Start with `server/cdn.py`,
  `server/aerialpacks.py`, `server/bnwt.py`, `server/decisions.py`, `server/dem.py` (`MT_CDN_*`), `server/imagery.py`
  (`bngs2`, `bngaer`) and `server/netfetch.py` (the host policies).
- **Builders:** `tools/build_s2_packs.py`, `tools/build_mt_packs.py`, `tools/build_aerial_packs.py`,
  `tools/aerial_ondemand.py`, `tools/build_bnwt.py`, `tools/build_decisions.py`. Run the tests with
  `tools/run_tests.py`.
- **Workflows, the switch file and the weekly data:**
  [TecnaGamer/BeamNG.Earth-data](https://github.com/TecnaGamer/BeamNG.Earth-data). Pull requests to the data repo
  are reviewed with extra care, because its jobs hold the bucket token: keep new code out of jobs with secrets, pin
  actions by SHA, and use only the standard library in upload jobs.
- **The collector:** `native/serverlist`. Changes to what it receives or stores must update
  [TELEMETRY.md](../TELEMETRY.md) first.
- **Deployment files:** [hosting/](../hosting/README.md).

Open an issue on GitHub or ask on Discord to suggest a change, report a broken file on the CDN, or propose a new
dataset. Good proposals say what the data is, its licence (it must allow redistribution), how big it is, how often it
changes, and what the client gains.

Never post secrets (tokens, account IDs, signing keys) in an issue, a pull request or a log. If you think one has
leaked, say so privately on Discord so it can be revoked.

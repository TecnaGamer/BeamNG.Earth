# hosting/

Files to run BeamNG.Earth's backend services on a machine of your own. The full picture (the tile bucket, the CDN,
the data builds, what each service does and why) is in [docs/HOSTING.md](../docs/HOSTING.md); this page is the
practical part.

| file | what it is |
|---|---|
| `deploy_serverlist.sh` | builds nothing; copies a built collector binary to the machine, installs or updates its systemd user unit and settings, restarts it and checks its endpoints |
| `systemd/bnge-serverlist.service` | the collector's unit (server list, telemetry, player reports, aerial requests) |
| `systemd/bnge-aerial-builder.service` | the on-demand aerial builder's unit (holds the bucket credentials) |
| `systemd/bnge-aerial-builder.path` | starts the builder once its credentials file exists |
| `aerial-builder/run.py` | the builder's service wrapper: credentials, disk guard, crash recovery |
| `systemd/bnge-decisions-builder.service` | the decision-archive builder's oneshot unit (no timer; started by hand) |
| `decisions-builder/run.py`, `decisions-builder/places.txt` | its wrapper (credentials, disk guard, cache cap, upload, pruning) and place list |
| `r2.env.example` | the credentials file's variable names, with placeholders only |

Everything runs as **systemd user units** of one ordinary user: no root service, no container. `%h` in the units is
that user's home folder.

## The machine

Any small always-on Linux box with a public address (or a tunnel) works; the project runs both services on one. The
collector needs very little; the builder uses up to 4 cores and 8 GB of RAM while it builds, and wants about 20 GB of
free disk.

Once, as root: `loginctl enable-linger <user>`, so the user's units start at boot without a login.

## The collector (native/serverlist)

1. **Build** it (on any machine):

   ```sh
   cd native/serverlist
   cargo build --release --target x86_64-unknown-linux-musl    # static: runs on any x86-64 Linux
   ```

   The musl target needs a C compiler for it (the project uses zig as CC). Or build on the machine itself with a plain
   `cargo build --release` and pass that binary as `BIN=...`.

2. **Deploy**:

   ```sh
   BOX=user@your-machine PUBLIC_URL=https://collector.example.org hosting/deploy_serverlist.sh
   ```

   Options: `AERIAL=off` (no aerial front door), `REPORTS_MAX_GB=2` (the reports' disk cap), `BIN=path`. The script
   keeps the previous binary as `bnge-serverlist.prev`; to roll back, move it back and
   `systemctl --user restart bnge-serverlist`. A restart empties the server list for about a minute, until the
   BeamMP servers announce themselves again.

3. **Put a reverse proxy with TLS in front.** The unit listens on `127.0.0.1:47640` only. With Caddy, for example:

   ```
   collector.example.org {
       reverse_proxy 127.0.0.1:47640
   }
   ```

   The collector takes a client's address from `X-Forwarded-For` only when the request comes from a local proxy, so
   nobody can list a server under someone else's address.

4. **Point clients at it** (they use the project's collector by default): `BNE_TELEMETRY_URL`,
   `BNGE_REPORT_URL`, `BNGE_AERIAL_REQUEST_URL` (docs/HOSTING.md, "Client settings"). The BeamMP server list address
   is fixed in `server/earthd.py` (`SERVER_LIST_URL`).

The collector downloads DB-IP's free IP-to-country table next to its binary and refreshes it weekly. Its settings:

| variable | default | meaning |
|---|---|---|
| `BNGE_LIST_ADDR` | `0.0.0.0:47640` (the unit sets `127.0.0.1:47640`) | where it listens |
| `BNGE_LIST_GEOIP` | `dbip-country-lite.csv.gz` next to the binary | the country table |
| `BNGE_TELEMETRY_DIR` | next to the binary | telemetry totals |
| `BNGE_TELEMETRY_TOKEN` | none (admin views off) | a private token for the admin views, made by the deploy script in `telemetry.env` (mode 600); never commit or share it |
| `BNGE_REPORTS_DIR` | `reports` next to the binary | player reports (private, kept 90 days) |
| `BNGE_REPORTS_MAX_GB` | 5 | disk cap for player reports, in GB (the deploy script sets 2) |
| `BNGE_AERIAL_SPOOL` | unset: aerial requests are refused | the spool folder the builder reads |

## The on-demand aerial builder

It takes the cells the collector spooled, builds their 1 m packs with `tools/build_aerial_packs.py` and uploads them
and the region's coverage file to the bucket. It is the only process that holds bucket credentials; the collector
never does.

On the machine, as the service user:

```sh
# 1. the code, copied from ONE BeamNG.Earth commit (a release tag is best)
mkdir -p ~/bnge-builder/aerial/tools ~/bnge-builder/aerial/server && chmod 700 ~/bnge-builder
#    from a checkout of that commit:
#    tools/aerial_ondemand.py tools/build_aerial_packs.py   -> ~/bnge-builder/aerial/tools/
#    server/tilepack.py server/dnp.py server/fast.py         -> ~/bnge-builder/aerial/server/
#    hosting/aerial-builder/run.py                           -> ~/bnge-builder/aerial/
#    and write the commit's SHA to ~/bnge-builder/aerial/COMMIT

# 2. its Python (3.12 works): numpy, Pillow >= 11.3 (AVIF), rasterio (GDAL), the aws CLI
python3 -m venv ~/bnge-builder/aerial/venv
~/bnge-builder/aerial/venv/bin/pip install numpy "Pillow>=11.3" rasterio awscli
~/bnge-builder/aerial/venv/bin/pip freeze > ~/bnge-builder/aerial/requirements.lock

# 3. the units
cp hosting/systemd/bnge-aerial-builder.service hosting/systemd/bnge-aerial-builder.path ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bnge-aerial-builder.path

# 4. the credentials (see r2.env.example); the path unit starts the builder within seconds
umask 077; cp r2.env.example ~/bnge-builder/r2.env; nano ~/bnge-builder/r2.env; chmod 600 ~/bnge-builder/r2.env
journalctl --user -u bnge-aerial-builder -n 20      # "aerial builder up"; "R2 credentials missing": a name is wrong
```

Check what it would do, without network or uploads:

```sh
~/bnge-builder/aerial/venv/bin/python ~/bnge-builder/aerial/run.py ~/bnge-builder/aerial-spool ~/bnge-builder/aerial-work --dry-run
```

The spool has five folders: `queue/`, `building/`, `done/`, `none/` (no open photos there) and `failed/`. An entry
is an empty file named `<x>_<y>` (a zoom-11 tile, about 15 km across); nothing about who asked is stored.

Settings in the unit: 6 worker processes, `Nice=15`, idle IO priority, `CPUQuota=400%`, `MemoryMax=8G`, and a disk
guard (`--min-free-gb 20`: no new cell starts with less free). A cell's work folder is about 60 MB and is removed
after the upload. A full cell is 64 + 21 packs, roughly 5 to 8 minutes at these limits.

**Pause:** `systemctl --user stop bnge-aerial-builder.path bnge-aerial-builder.service` (stop the path unit too, or it
starts the service again). **Off at boot:** `systemctl --user disable bnge-aerial-builder.path`.
**Update:** copy the five files from a new commit, update `COMMIT`, `systemctl --user restart bnge-aerial-builder`.

## The decision-archive builder

It builds the signed decision archives (docs/HOSTING.md, "Signed decision archives") of the places in `places.txt`
and uploads them. It holds the signing key, which never leaves the box.

```sh
# on the box, from a checkout of the release commit C:
mkdir -p ~/bnge-builder/decisions && cd ~/bnge-builder/decisions
mkdir -p src && git -C <repo> archive C server tools/build_decisions.py docs/PLACES.md | tar -x -C src
echo C > COMMIT
cp <repo>/hosting/decisions-builder/run.py <repo>/hosting/decisions-builder/places.txt .
python3 -m venv venv && venv/bin/pip install -r src/server/requirements.txt awscli
cp <repo>/hosting/systemd/bnge-decisions-builder.service ~/.config/systemd/user/ && systemctl --user daemon-reload
```

**Run** after each release and after each weekly data build: `systemctl --user start bnge-decisions-builder`, then
`journalctl --user -u bnge-decisions-builder -f` (it ends with `uploaded <vkey>: manifest of N blocks`). The unit has no
`[Install]` section and there is no timer. Settings: `--procs 4`, `Nice=15`, idle IO priority, `CPUQuota=400%`,
`MemoryMax=12G`; no place starts with less than 20 GB free or with the cache over 15 GB. **After a release:**
`venv/bin/python run.py prune-out --keep <new vkey>` (through the unit's environment to remove retired files from the
bucket too). **Off:** leave `decisions` out of the switch file; the builder only runs when started.

## Keep secrets off the repo

Never commit `r2.env`, `telemetry.env` or a decision signing key, and never put their values on a command line, in a
log or in an issue. The scripts here read them only from files with mode 600 and never print them.

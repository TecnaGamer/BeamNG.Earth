# weathersynth

BeamNG.Earth's weather sounds made from physics: thunder from a tortuous lightning channel and rain from drop impacts
and bubbles. It renders the mod's `.ogg` assets offline (deterministic, seeded). Behind the `audio` feature it also has
a live engine for the world server. The commands are listed at the top of `src/main.rs`.

## Builds

The offline renderer needs only `libm` (pinned, pure Rust):

```
cargo build --release --locked
```

The live audio output (`--features audio`) adds cpal 0.17.3 with default features only: ALSA on Linux, and WASAPI
shared mode on Windows (windows 0.62.2). Every crate in that set was read before it came in (2026-11-25), and
`Cargo.lock` pins each one's crates.io checksum. Any fetch therefore gets those exact sources or fails.

### Normal and CI builds (no vendor folder)

```
cargo fetch --locked --target x86_64-unknown-linux-gnu      # only that target's crates (the reviewed set)
cargo build --release --locked --features audio
```

On Windows, fetch with `--target x86_64-pc-windows-gnu` (or msvc). Fetching without `--target` would also download
cpal's macOS, iOS, Android and wasm dependencies. They are never compiled for our targets, but they were not reviewed.

### Offline builds from the reviewed crates

`vendor/` is not committed (it is about 126 MB, mostly the windows crate). Rebuild it from cargo's download cache:

```
python3 -I tools/vendor_reviewed.py Cargo.lock vendor
cargo build --release --offline --locked --features audio --config vendor/config.toml
```

The tool does the following:
- **Reviewed crates:** checks each tarball's sha256 against the crates.io index and extracts the crates unmodified,
  with their `.cargo-checksum.json`.
- **Other packages in the lock:** writes stubs for cpal's other-platform dependencies, which cargo resolves across
  every platform. Each stub has the real crate's name, version, dependencies and features, a lib that is a
  `compile_error!`, and a `build.rs` that panics when the crate declares `links`. A stub can never be built by
  accident.
- **`vendor/config.toml`:** writes the source replacement, passed explicitly with `--config`. Nothing in the
  repository forces it.

### Release targets

- **Linux, glibc 2.35 or older** (like earthfast and the AppImage):

  ```
  CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C link-arg=-Wl,--allow-shlib-undefined" \
    cargo zigbuild --release --locked --features audio --target x86_64-unknown-linux-gnu.2.35
  ```

  - alsa-sys links `libasound.so.2` dynamically, so at runtime the binary uses the system's own libasound.
  - `--allow-shlib-undefined` is needed only because the build machine's libasound was built against a newer glibc
    than the 2.35 stubs (`__isoc23_vsscanf@GLIBC_2.38`). The binary itself needs at most GLIBC_2.34
    (`objdump -T`).
  - Building needs alsa-lib's development files (`pkg-config alsa`).
- **Windows:** `cargo zigbuild --release --locked --features audio --target x86_64-pc-windows-gnu`. The binary imports
  only system DLLs. No WinRT activation function is imported, and cpal uses Win32 COM (`CoCreateInstance`) only.

## Live weather sounds (`live-serve`)

`weathersynth live-serve [--thunder procedural|files:DIR]` is started by the world server (server/liveaudio.py) when
the Weather page's "Weather sounds" is Live.
- **Input:** the mod's `weather_audio` messages on stdin, one JSON line each (src/proto.rs; server/protocol_table.json).
- **Output:** a state line on stdout every 0.5 s (`live` with the output latency and the thunder counters), or one
  `failed` line before a non-zero exit. The server forwards these as `audio_state`.
- **Fallback:** the mod plays its pre-recorded files whenever the state lines stop.
- **Quiet:** when muted, when the input stops for 1 s, or on Pre-recorded.
- **Exit:** fades out and exits on EOF, or by itself (code 0) after `LINGER_S` (60 s) without a Live line. The
  server forwards Pre-recorded rather than closing the pipe, so a switch back to Live within a minute keeps the same
  process (the 2026-11-26 playtest's three starts in two minutes were such switches).
- **Rain:** off while the mod says `synthRain: false` (the default since 2026-11-26: Live plays the game's own rain
  loops and the synth only the thunder and its ducks). When it plays, the open-air mix is capped at the loudness the
  game plays its loops at for the rate (live.rs `RainCap`: SOURCES.md's loop loudness x RAIN_GAIN x the rain share;
  uncapped the drops ran 20-30 dB hotter: 35 mm/h +6.5 LUFS against the loops' -23.2).

Thunder (src/thunderlive.rs):
- **Two messages per strike.** The mod sends a look-ahead hint up to 3 s early, rendered at once on its own thread,
  and the final one at the strike. Only finals play, at their UTC arrival time minus the output latency.
- **The renderer is a trait.** `ThunderRenderer::render(&Spec { seed, d, kind }) -> mono f32 at 44.1 kHz`.
  `procedural` is thunder.rs's model, set to each class's loudness target. `files:DIR` plays
  `bne_thunder_<kind><n>.wav` picked by the seed. Another method only needs a new impl and a `--thunder` name.

Output latency (src/audio.rs, serve.rs):
- **Device buffer:** cpal is asked for a fixed buffer (`--buffer`; 1024 frames on Linux since 2026-11-26, 256
  elsewhere; twice that when the machine is busy at the start), clamped to the device's range. WASAPI shared mode
  ignores the request and calls back with its own ~22 ms buffer.
- **Fill target:** one callback plus max(one callback, 2048 frames), and half a callback more per ring underrun (at
  most 0.25 s). About 0.1 s in all: the thunders are scheduled on the UTC clock with the latency taken off.
- **Priority** (prio.rs, best effort): the worker thread (and on Linux the device's callback thread) asks for nice -11,
  -6 or -2, whichever the system allows (RLIMIT_NICE); on Windows the worker runs at THREAD_PRIORITY_HIGHEST. Never a
  realtime policy. The device line in the server log says what it got.
- **The callback allocates nothing** (`audio::tests::the_callback_path_never_allocates`, a counting allocator).
- **Trouble:** a stream error (ALSA POLLERR after an unrecovered xrun, a device lost), or 3 xruns within 60 s, reopens
  the default device with twice the buffer, up to 2048 frames. After 5 quiet minutes the buffer halves again, never
  below the first size, except on a machine with 8 hardware threads or fewer (the Deck), which keeps it: each reopen is
  a glitch (the Deck flipped 512 / 1024 six times in the 2026-11-26 broker run). Only failures at 2048 frames count
  toward `failed` (3 a minute).
- **Thunder renders** (thunderlive.rs): the render thread runs at LOW priority (nice 10, Windows below normal) so method
  B (seconds per strike) never competes with the game. At most one costly render (a generated strike or close) waits
  or runs at a time; another hint meanwhile, or any costly render while the machine is busy (the 1-minute load above
  1.0 per cpu), is not rendered and its strike plays from the file (`skipped` in the state line). The grain bank's
  renders (tens of ms) are not limited.

Ten-minute soaks (2026-11-25; silent, volumes 0; rain at 4 Hz, a hinted thunder every 30 s):

| Machine | Priority, load | Device | Latency min / p50 / p95 / max | Underruns | xruns |
|---|---|---|---|---|---|
| PC (CachyOS, PipeWire) | nice 19, a 13-core job at nice -4 | ALSA, 512 frames | 34 / 50 / 56 / 56 ms | 0 | 0 |
| Windows 11 test PC | normal | WASAPI 48 kHz, 1056-frame callbacks | 44 / 54 / 54 / 54 ms | 0 | 0 |
| Deck, Game Mode, broker game running (it at nice 10) | nice 19, load 8-10 on 8 threads | ALSA (PipeWire); 512, grew to 1024 | 50 / 99 / 132 / 134 ms | 0 | 7 |
| Deck, same | nice 0, load 7-9 | ALSA, 512 frames | 51 / 71 / 77 / 77 ms | 0 | 0 |

On the Deck a nice-19 process gets little CPU next to a nice-10 game at load 10, and it cannot raise its own priority
(RLIMIT_NICE 0). A player's server, and so the synth, runs at normal priority.

The thunder renderer (procedural) took up to 1.9 s per strike on the PC and Windows, and up to 3.5-4.3 s on the loaded
Deck. It runs on its own thread, so the output never waits for it. On the Deck, 2 of 20 thunders started late
(their renders took longer than the 3 s look-ahead). Not yet checked: the Deck in Desktop
Mode, and anything with the game in the loop (the broker).

## Thunder from the grain bank (src/grainbank.rs; method A: the Recorded thunder set, bank v3)

The Recorded thunder set is built from 97 short CC0 field recordings ("grains": bodies, cracks, claps, rolls and mid /
far rolls, cut from 23 Freesound originals), layered and spread in time by a random lightning channel. The behaviour
follows `~/bnge-tests/weather/procedural/grainbank/v3/SPEC.md` (sections 6-7), ported from its Python reference
renderer (render_v3.py; the user approved its references on 2026-11-25).

- **The same schedule as the reference:** the draws go through `nprng` (numpy's Generator, bit for bit), so a seed
  picks the same grains, times and gains as render_v3.py: 40 of 40 stats renders (seeds 101-110 per class) schedule
  identically, the samples differ by about 3.7e-3 of the peak (the absorption kernels are cut short and the grains are
  i16), and every class passes the spec's acceptance with medians at the reference's
  (`~/bnge-tests/weather/procedural/finalA_v3/accept_v3.py`; score.py's jag and first300 are measured on Ogg q6 as the
  references were: on a WAV, an onset from silence is one bigger step).
- **The sources (committed):** `assets/grains/` is the cutting tool's output as it comes: `manifest.json`,
  `grains/*.ogg` (1.8 MB, Vorbis q5) and `CREDITS.md` (the 23 recordings and their recordists). A re-cut from the
  originals replaces that folder.
- **The bank (built, gitignored):** `assets/thunder_grains.bin` ("WSGRAIN2"), raw i16 PCM with a small header (each
  grain's kind and the class of the recording it came from) and an FNV checksum, so no Vorbis decoder is involved at
  run time. Build it with `weathersynth grainbank-pack`, run in this folder (the defaults are
  `--manifest assets/grains/manifest.json --out assets/thunder_grains.bin`).
- **How the pack decodes:**
  - ffmpeg's own Vorbis decoder, named explicitly (`-c:a vorbis`, not libvorbis);
  - its float output taken as is (`-f f32le`; the grains are mono 44.1 kHz, so nothing is remixed or resampled);
  - the conversion to 16 bits done in Rust (x 32767, rounded half away from zero, clamped).
- **The decoder as the only variable:** `assets/grains/thunder_grains.bin.sha256` is the bank packed with ffmpeg
  n9.0.2. The release workflow packs once (Ubuntu 22.04's ffmpeg) and warns when its sha256 differs; both platforms'
  apps then bundle that one file. A different decoder build moves samples by at most a few LSB.
- **v3's rules on top of v1's scheduling:** only the distance beyond each grain's own recording (k_rec by the source's
  class: 250 / 1000 / 3500 / 9000 m) is filtered, times the class's abs_scale (1 / 0.1 / 0 / 0.3); far rolls draw
  from every roll pool; the rumble balance shelves (low at 150-250 Hz, high at 1 kHz) crossfade in from 0.6 s after
  the first arrival.
- **Offline:** `weathersynth grains-set --bank assets/thunder_grains.bin --out DIR --ogg [--seeds 1,2,...]
  [--prefix bne_thunder_rec_]` renders `<prefix><class><k>`, each levelled to its class target with a sample peak at
  most -1 dBFS. `grains-render ... --class C --seed N [--dist M] [--live]` renders a single strike.
- **Live:** `live-serve` plays the Recorded set through this renderer; it reads the bank from next to the program, or
  from `assets/` in a source tree. A strike renders in tens of ms.
- **Determinism:** nprng, libm maths and f64 mixing in a fixed order; absorption through minimum-phase kernels designed
  per distance in 1 % steps (a kernel depends only on its key, so the cache never changes the output).

### Cutting the bank from the originals (src/grainbuild.rs; bank v3)

`grainbank-build` turns the CC0 originals into grains, following
`~/bnge-tests/weather/procedural/grainbank/v3/SPEC.md` sections 1-5. It is a port of the research tools
(`build_bank.py` with `BANK=v3`, `gbdsp.py`, and the screening in `wxlib.py`). The bank folder needs `sources.json`,
`originals/fs<ID>.*` and `originals.sha256`.

```
weathersynth grainbank-build plan --gb DIR [--plan DIR/plan_v3.json] [--threads N] [--verbose]
weathersynth grainbank-build cut  --gb DIR [--plan DIR/plan_v3.json] --out BANKDIR [--threads N]
weathersynth grainbank-compare --ref REF/manifest.json --new BANKDIR/manifest.json
```

- **plan:** finds and screens the events in each original, then denoises them and checks the residue, the rain
  contamination and clipping. It lists the candidate cuts and selects them by score, budget and per-source cap.
- **cut:** cuts each planned grain. Wet takes get the Wiener denoise and the high-band gate, cracks a 700 Hz high-pass,
  peaky bodies pre-compression. Each grain is faded, levelled to -18 dBFS (loudest 400 ms) and written as Vorbis q5
  (`BANKDIR/grains/<id>.ogg`, through ffmpeg with `+bitexact`). `BANKDIR/manifest.json` uses the reference's schema.
- **Inputs are checked:** every original's sha256 must match `originals.sha256` before ffmpeg decodes it. A missing
  or wrong entry stops the build.
- **Deterministic:**
  - libm maths and sums in a fixed order;
  - sources and grains in the input files' order;
  - `--threads` only splits independent sources, and the results go back in source order.
- **Against the Python reference (2026-11-25, 79 originals):**
  - the plan log is identical line for line, and the plan is identical field for field (scores within 2e-13);
  - all 97 grains decode bit-identical to the reference's `v3/grains`;
  - the manifest is text-identical except `sha256_grain`. The reference's Ogg files carry a random stream serial and
    an encoder tag.
  - The run takes 22 s (plan) and 8 s (cut) with 4 threads, on the PC.

## Recorded rain, wind, drips and hail (src/texbuild.rs, texmix.rs, livetex.rs)

The Recorded rain set and the wind, drip and hail loops use the same idea as the Recorded thunder: clean pieces of
CC0 Freesound field recordings, mixed with controlled variation. Rain and wind are textures, so the pieces are long
clean stretches ("blocks"), not events.

- **The bank:** built from the originals with `texbank-build`. The 5 s windows are screened with the thunder
  builder's rules plus the rain-only ones (traffic swell, unsteady, <120 Hz rumble, knocks). Extra checks:
  - thunder blanked;
  - microphone buffeting (wind);
  - still raining (drips);
  - one hit far above its window's level (crest).
  - Clean runs of 10 s or more become blocks.
  - A density check keeps light rain sparse (2.5-6 kHz envelope kurtosis >= 8) and heavy rain dense (<= 40).
  - Each block is high-passed and levelled to -30 LUFS, then the blocks are selected round-robin over the recordings
    within a budget and a cap per recording.
  - Car takes keep the intensity their title states, or are split into light / medium / heavy by kurtosis.
  - Output: `tex_bank.bin` ("WSTEX1", i16), `manifest.json`, `screening.tsv`, `blocks_fit.tsv` and `CREDITS.md`.
  - The screening is cached per original's sha256 (`--cache`).
- **The loops:** `tex-set` renders them. Each is two streams of U(2, 5) s segments from random places, with varispeed
  U(0.97, 1.03), +/-1 dB of gain and 0.5 s equal-power joins.
  - The streams are laid on a circular timeline, so the wrap is a crossfade like any other join.
  - Sparse classes keep the second stream 6 dB down.
  - Each loop is levelled to its class (the current loops' loudness: rain -30.2 / -24.2 / -17.2, roof -30.0 / -24.1 /
    -17.2, muffled -25.9; wind -36 / -30 / -24, drips -34, hail -20 LUFS).
  - A circular 5 ms look-ahead limiter runs only when a hit passes -1 dBTP.
  - `tex-check` measures each loop's loudness, its true peak and its seam (the 50 ms step and the spectral flux
    across the wrap, ranked among the same windows inside the loop).
- **Live:** livetex.rs plays the same blocks continuously when the mod sends `rainSet: "recorded"`.
  - The layers are outdoor light / medium / heavy and the roof's three, with equal-power weights in log rate. The
    muffled path is two 900 Hz low-passes, levelled as the muffled loop.
  - Every layer plays at its loop's loudness times the mod's `rainVol`, so Live matches Pre-recorded.
  - A silent layer costs nothing.
  - The output does not depend on the block size (test).
  - `texbank-live` cuts the live pieces from the full bank: 42 s per class, round-robin over the recordings.
    They are committed like the thunder grains, in `assets/tex/` (`manifest.json`, `blocks/*.ogg` at Vorbis q6,
    4.6 MB, and `CREDITS.md`).
  - `texbank-pack` decodes them into `assets/tex_bank.bin` (about 33 MB of i16, gitignored). It uses ffmpeg's own
    Vorbis decoder, as `grainbank-pack` does; `assets/tex/tex_bank.bin.sha256` is the pack made with ffmpeg n9.0.2.
  - The release's grains job packs it once for both platforms, and live-serve reads it from next to the program.
  - `livetex-bench` renders fixed conditions and a sweep, timed: about 0.65 % of one PC core.
- **After the user's listen (2026-11-26):**
  - **Light rain is variant B:** its crest limit is 40 dB, so the sparse takes of close drops are in.
  - **Fewer take changes:** segments are 4-12 s with 1 s joins and ±1 % varispeed (they were 2-5 s, 0.5 s and ±3 %).
    `--legacy` renders the old way.
  - **One gain per recording:** a take keeps its level from block to block.
  - **A spectral check per class:** a recording more than 7.5 dB RMS off its class's median 1/3-octave shape is left
    out (`spec_dev.tsv`); light rain is exempt.
  - `tex-set` prints the take changes per minute and the timbre jump across them, against the same measure inside a
    segment.
  - **The roof:** outside, the plain loops near the car; inside, `roof_cabin_*` (two 1.4 kHz low-passes and a 140 Hz
    body resonance).
  - **Muffled:** A (the outdoor rain filtered) under enclosed shelters; B (`muffled_open`, the under-bridge takes)
    under open ones.
  - **Drips:** `drips_1/2/3` are single drops (the bank's `drip` grains, cut from distinct-drop takes) at 0.8, 0.3 and
    0.1 per second, all at one gain, so only the rate falls as the mod crossfades them.
  - **Live:** livetex follows all of this through the new `roofNear` and `enclosed` fields. Its blocks are levelled at
    -30 LUFS each, and a segment never outgrows its blocks.
- **After the second listen (2026-11-26):**
  - **Drips:** the recorded drops carried their take's noise in and out.
    - `drips_1/2/3` are now synthetic: rain.rs's drop model, with big slow drops onto a puddle (with a Minnaert
      bubble), the road or leaves. There is exact silence between drops.
    - `drips_rec_1/2/3` keep the recorded drops for an A/B, cleaned by `impact_grains`. The take's noise is measured
      in the gap just before each hit and subtracted, then the grain is gated to exact zeros 40 dB under its peak.
  - **Wind:** it sounded like rain.
    - `patter.tsv` measures every window's 2-8 kHz share. Rain, leaf patter and hiss measure about -9 to -3 dB; wind
      measures -14 to -30 dB.
    - Wind windows above -12 dB are rejected.
  - **Hail:** it sounded like rain.
    - `hail_hit` grains are bright, isolated hits cut from the hail takes and cleaned.
    - The hail loop schedules them as sparse hard hits (7 per second, about a third bouncing) over a little rain,
      14 dB under the hits.
    - `hail_texture` is the old wash, kept for comparison.
- **The procedural live rain's level:** `live::file_match_db` brings it to the files' level. Before, it was 13.5 /
  22.3 / 24.0 dB louder at 0.7 / 5 / 35 mm/h than the loop the mod plays at the same volume, and it had no `rainVol`.
  The bus limiter crushed it.

## Audio rules (src/audio.rs)

- **Output streams only.** On WASAPI, an input stream opened on an output device is loopback capture of the system's
  audio. A unit test fails if the input-stream builder is ever named in `audio.rs`.
- **The callback only copies.** On Windows, cpal's audio thread runs at THREAD_PRIORITY_TIME_CRITICAL. The synthesis
  runs on a worker thread into a lock-free single-producer / single-consumer ring; on an underrun the callback plays
  zeros and never waits.
- **No WinRT classes.** windows-core's registration-free WinRT fallback would search for `<Namespace>.dll`; it is
  linked but never reached.

## Checks

- `cargo test --release --locked --features audio`: the ISO 9613-1 table, the N-wave spectrum, output-only streams,
  the audio ring, the JSON line reader, the thunder bus (hint / final / late / kind change) and the live mix.
- `weathersynth storm-dump` against `tools/storm_dump.lua`, run under LuaJIT on the weather branch's storm.lua. The
  strike lattice must match byte for byte.
- `weathersynth live-bench` and `thunder-bench`: the live engine's CPU cost, with no device.

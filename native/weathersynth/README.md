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
- **Exit:** fades out and exits on EOF.

Thunder (src/thunderlive.rs):
- **Two messages per strike.** The mod sends a look-ahead hint up to 3 s early, rendered at once on its own thread,
  and the final one at the strike. Only finals play, at their UTC arrival time minus the output latency.
- **The renderer is a trait.** `ThunderRenderer::render(&Spec { seed, d, kind }) -> mono f32 at 44.1 kHz`.
  `procedural` is thunder.rs's model, set to each class's loudness target. `files:DIR` plays
  `bne_thunder_<kind><n>.wav` picked by the seed. Another method only needs a new impl and a `--thunder` name.

Device checks so far (2026-11-25, silent: volumes 0):

| Machine | Device / format | Latency | Underruns |
|---|---|---|---|
| PC (CachyOS, PipeWire, native binary) | ALSA default, 44.1 kHz f32 stereo | 158-174 ms | 0 |
| Deck (Game Mode, glibc 2.35 build, a game running) | ALSA default (PipeWire), 44.1 kHz f32 | 176-194 ms | 0 |
| Windows 11 test PC (over SSH) | WASAPI shared, 48 kHz f32 | 138-148 ms | 0 |

Two thunders hinted 3 s ahead rendered and played in time on the PC and on Windows. Not yet checked: the Deck in
Desktop Mode, and anything with the game in the loop (the broker).

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

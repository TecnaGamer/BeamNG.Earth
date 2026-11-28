"""build_airtraffic_sounds.py: the live air traffic's aircraft sound loops (mod/.../art/sound/airtraffic/*.ogg).

A port of devo343's tools/build_aircraft_sounds.py from their Live Air Traffic add-on (used with their permission):
the same synthesis, the same seed and the same order of random draws, so it makes their WAVs sample for sample
(--compare checks that), then Ogg Vorbis for the mod.

Every sound is made only of parts that repeat exactly over the loop (tones on whole multiples of 1/LOOP_S Hz, noise
shaped in the frequency domain, pulse trains placed round a circle, filtering done on the circular spectrum), so each
file loops with no seam and no click. Two layers per kind of aircraft, crossfaded by the mod with distance:

  near     the full sound up close: roar, fan / turbine whine, propeller or rotor buzz
  far      what is left a few km off: the air soaks up the high frequencies, a low rumble or drone remains
  distant  (jets) the deep rolling rumble of a jet high overhead or far off (16 s loop): the synthesised one (the
           add-on's recording of a distant plane is not used: its source does not allow passing it on)

Kinds: jet (airliners and business jets, pitched per type in the game), prop (turboprops: two propellers a little
out of step, so they beat), piston (light aircraft), heli (rotor slap, tail rotor, turbine).

lat_jet_close.ogg is the add-on's lat_jet_close.wav (a recording, a seamless 15 s loop: Pixabay, tanweraman, "big
plane sound effect", Pixabay Content Licence) converted as it is.

    python3 tools/build_airtraffic_sounds.py [--src DIR] [--compare DIR]

DIR: the add-on's art/sound/liveAirTraffic folder (default under ~/bnge-tests/pr1580src). Needs numpy and ffmpeg
(libvorbis). Writes mono Ogg Vorbis (quality 4, 22,050 Hz), bit-exact (the same bytes on every run).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import wave

try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:     # (loaded by its path into another process, a test: that process's entry point guards)
    pass
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "sound", "airtraffic")
SRC = os.path.expanduser("~/bnge-tests/pr1580src/src/live-air-traffic/mod_src/levels/beamng_earth/art/sound/"
                         "liveAirTraffic")

SR = 22050
LOOP_S = 6.0
N = int(SR * LOOP_S)
T = np.arange(N) / SR
FREQS = np.fft.rfftfreq(N, 1 / SR)
TARGET_RMS = 10 ** (-17 / 20)     # every file equally loud (the game sets the real loudness)
rng = np.random.default_rng(20261123)


def snap(f):
    """The nearest frequency that repeats exactly over the loop."""
    return round(f * LOOP_S) / LOOP_S


def noise(shape):
    """Periodic noise with the amplitude spectrum shape(f)."""
    amp = shape(np.maximum(FREQS, 1e-3))
    amp[0] = 0
    spec = amp * np.exp(1j * rng.uniform(0, 2 * np.pi, len(FREQS)))
    x = np.fft.irfft(spec, N)
    return x / (np.sqrt(np.mean(x ** 2)) + 1e-12)


def filt(x, shape):
    """Circular (loop-safe) filtering by the gain curve shape(f)."""
    g = shape(np.maximum(FREQS, 1e-3))
    g[0] = 0
    return np.fft.irfft(np.fft.rfft(x) * g, N)


def tone(f, phase=None):
    return np.sin(2 * np.pi * snap(f) * T + (rng.uniform(0, 2 * np.pi) if phase is None else phase))


def harmonics(f0, count, slope=1.0, jitter=0.35):
    """A harmonic series on f0 (a buzzy periodic engine / propeller sound)."""
    x = np.zeros(N)
    for k in range(1, count + 1):
        if snap(f0 * k) >= SR / 2 - 200:
            break
        x += (1 / k ** slope) * (1 + jitter * rng.uniform(-1, 1)) * tone(f0 * k)
    return x


def am(depth, cycles):
    """Slow periodic loudness changes (turbulence, the air between): whole numbers of cycles over the loop."""
    m = np.zeros(N)
    for c in cycles:
        m += rng.uniform(0.5, 1.0) * np.sin(2 * np.pi * c * T / LOOP_S + rng.uniform(0, 2 * np.pi))
    m /= max(1e-9, np.max(np.abs(m)))
    return 1 + depth * m


def pulses(rate, shape_len_s, decay_s, jitter=0.0, body=None):
    """A periodic train of short decaying bursts (rotor slap, piston firing)."""
    count = int(round(rate * LOOP_S))
    L = int(shape_len_s * SR)
    t = np.arange(L) / SR
    out = np.zeros(N)
    for i in range(count):
        start = int(round(i * N / count))
        burst = rng.normal(0, 1, L) * np.exp(-t / decay_s)
        if body is not None:
            burst += body(t) * np.exp(-t / (decay_s * 1.5))
        burst *= 1 + jitter * rng.uniform(-1, 1)
        idx = (start + np.arange(L)) % N
        out[idx] += burst
    return out


def lp(fc, order=2):
    return lambda f: 1 / np.sqrt(1 + (f / fc) ** (2 * order))


def hp(fc, order=2):
    return lambda f: 1 / np.sqrt(1 + (fc / f) ** (2 * order))


def bp(lo, hi, order=2):
    return lambda f: lp(hi, order)(f) * hp(lo, order)(f)


def norm(x):
    return x / (np.sqrt(np.mean(x ** 2)) + 1e-12)


# ------------------------------------------------------------------------------------------------ the sounds
def jet_near():
    roar = noise(lambda f: np.where(f < 350, (f / 350) ** 0.8, (350 / f) ** 1.15) * hp(35)(f))
    fan = 50.0                                     # fan shaft (N1) about 3,000 rpm
    buzz = filt(harmonics(fan, 60, slope=0.55, jitter=0.6), bp(200, 3500))   # the "buzz-saw" of the fan tips
    whine = 0.6 * tone(fan * 24) + 0.25 * tone(fan * 48) + 0.12 * tone(fan * 24 * 1.003)  # blade-pass whine
    x = 1.0 * roar + 0.28 * norm(buzz) + 0.22 * norm(whine)
    return x * am(0.18, [2, 3, 5, 7])


def jet_far():
    rumble = noise(lambda f: np.where(f < 85, (f / 85) ** 1.0, (85 / f) ** 1.5) * hp(40)(f) * lp(420, 2)(f))
    return rumble * am(0.45, [1, 2, 3])           # the slow roll of a far jet through moving air


def prop_near():
    a, b = 102.0, 103.0                            # two propellers, a little out of step: 1 Hz beating
    blades = norm(harmonics(a, 30, slope=0.85)) + 0.85 * norm(harmonics(b, 30, slope=0.85))
    turbine = 0.5 * tone(3700) + 0.2 * tone(7400) + 0.15 * tone(3712)
    wash = noise(lambda f: lp(900)(f) * hp(40)(f))
    x = 0.75 * norm(blades) + 0.18 * norm(turbine) + 0.45 * wash
    return x * am(0.08, [3, 5])


def prop_far():
    a, b = 102.0, 103.0
    drone = filt(harmonics(a, 6, slope=0.6) + 0.9 * harmonics(b, 6, slope=0.6), lp(260, 3))
    rumble = noise(lambda f: lp(140, 2)(f) * hp(30)(f))
    return (0.8 * norm(drone) + 0.4 * rumble) * am(0.3, [1, 2])


def piston_near():
    fire = 80.0                                    # 4 cylinders at 2,400 rpm; a two-blade propeller at 80 Hz too
    bangs = pulses(fire, 0.012, 0.0035, jitter=0.35)
    bangs = filt(bangs, bp(60, 1600))
    prop = harmonics(fire, 20, slope=1.0)
    wash = noise(lambda f: lp(1200)(f) * hp(50)(f))
    x = 0.5 * norm(bangs) + 0.7 * norm(prop) + 0.35 * wash
    return x * am(0.06, [4, 9])


def piston_far():
    fire = 80.0
    drone = filt(harmonics(fire, 6, slope=0.7) + 0.5 * pulses(fire, 0.008, 0.002, jitter=0.4), lp(220, 3))
    return (norm(drone) + 0.3 * noise(lambda f: lp(150)(f) * hp(30)(f))) * am(0.3, [1, 2, 3])


def heli_near():
    slap = pulses(13.0, 0.06, 0.012, jitter=0.15, body=lambda t: 3 * np.sin(2 * np.pi * 55 * t))   # main rotor
    slap = filt(slap, bp(30, 1800))
    tail = filt(harmonics(86.0, 25, slope=0.8), bp(80, 3000))                                       # tail rotor
    turbine = 0.4 * tone(6100) + 0.2 * tone(6122)
    wash = noise(lambda f: lp(700)(f) * hp(30)(f))
    return 1.0 * norm(slap) + 0.35 * norm(tail) + 0.08 * norm(turbine) + 0.35 * wash


def rotor_chops(rate, strong=1.0, weak=0.72, tau=0.008, body_hz=70.0, body_decay=0.015, slap=0.6, jitter=0.08):
    """The blade slap of a two-blade rotor: one sharp pressure pulse (an N-wave: a dip then a push, the "crack")
    with a short low thump behind it, per blade passing; the two blades alternate a little stronger and weaker
    ("wop-wop"). Exactly periodic over the loop (an even number of chops)."""
    count = int(round(rate * LOOP_S))
    count += count % 2
    L = int(0.07 * SR)
    t = np.arange(L) / SR
    nwave = np.where(t < tau, -np.sin(2 * np.pi * t / tau), 0.0)
    body = np.sin(2 * np.pi * body_hz * t) * np.exp(-t / body_decay)
    out = np.zeros(N)
    for i in range(count):
        start = int(round(i * N / count))
        hiss = filt_short(rng.normal(0, 1, L)) * np.exp(-t / 0.006)
        chop = 1.0 * nwave + 0.8 * body + slap * hiss
        chop *= (strong if i % 2 == 0 else weak) * (1 + jitter * rng.uniform(-1, 1))
        out[(start + np.arange(L)) % N] += chop
    return out


def filt_short(x):
    """A quick band-pass (150-900 Hz) for a short burst (not circular: it is placed by the caller)."""
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    g = bp(150, 900)(np.maximum(f, 1e-3))
    return np.fft.irfft(X * g, len(x))


def heli_far():
    """A helicopter a few km off: little but the rotor's chop is left, the "wop-wop-wop" that carries for miles.
    Clean chops with quiet between them, band-limited the way distance leaves them (60-450 Hz), slow swells as the
    sound comes through moving air. A 12 s loop so the swells do not repeat noticeably."""
    chops = filt(rotor_chops(13.0), bp(55, 450, 2))
    swell = am(0.35, [1, 2, 3, 5])
    bed = noise(lambda f: lp(200)(f) * hp(45)(f))
    return norm(chops) * swell + 0.05 * bed


def comb_sweep(x, d_min_s, d_max_s, depth, cycles=(1, 2)):
    """A slowly sweeping comb filter (circular, loop-safe): the sound plus a copy of itself delayed by a slowly
    changing time. Outdoors this is the ground reflection: the direct sound and the one bounced off the ground arrive
    a few milliseconds apart, and as the aircraft moves across the sky that gap changes, sweeping notches up and down
    the roar: the "whooshing" phase of a jet passing high overhead."""
    m = np.zeros(N)
    for k, c in enumerate(cycles):
        m += (1.0 / (k + 1)) * np.sin(2 * np.pi * c * T / LOOP_S + rng.uniform(0, 2 * np.pi))
    m = (m - m.min()) / (m.max() - m.min())
    delay = (d_min_s + (d_max_s - d_min_s) * m) * SR          # samples, slowly varying
    pos = np.arange(N) - delay
    i = np.floor(pos).astype(int)
    f = pos - i
    delayed = x[i % N] * (1 - f) + x[(i + 1) % N] * f
    return x + depth * delayed


def jet_distant():
    """A jet high overhead (cruise, 30-40,000 ft): what reaches the ground is a soft, airy roar, like far-off wind or
    a distant waterfall (most of it 100-600 Hz, the whine long gone), with a slow phasing "whoosh" as the direct and
    ground-reflected sound drift in and out of step, gentle swells as it comes through moving air, and a faint
    crackle in the louder moments. A 16 s loop so none of it repeats noticeably."""
    roar = noise(lambda f: np.where(f < 180, (f / 180) ** 1.0, (180 / f) ** 0.9) * hp(40, 2)(f) * lp(1800, 2)(f))
    air = noise(lambda f: bp(400, 2500, 1)(f))                 # the "shhh" on top
    x = norm(roar) + 0.22 * norm(air)
    x = comb_sweep(x, 0.0015, 0.009, 0.85)
    # gentle, uneven swells (no quiet gaps: high up, the roar is steady and only breathes)
    m = np.zeros(N)
    for c in (1, 2, 3, 5):
        m += rng.uniform(0.4, 1.0) / c ** 0.6 * np.sin(2 * np.pi * c * T / LOOP_S + rng.uniform(0, 2 * np.pi))
    m = (m - m.min()) / (m.max() - m.min())
    swell = 0.6 + 0.4 * m ** 1.2
    crackle = noise(lambda f: bp(200, 1200, 2)(f)) * (m ** 3)
    return norm(x) * swell + 0.08 * crackle


SOUNDS = {"jet": (jet_near, jet_far), "prop": (prop_near, prop_far), "piston": (piston_near, piston_far),
          "heli": (heli_near, heli_far)}


def write(path, x):
    x = norm(x) * TARGET_RMS
    peak = np.max(np.abs(x))
    if peak > 0.97:                                # soft limit the odd peak (rotor slap) rather than clip
        x = np.tanh(x / 0.97 * 1.2) * 0.97 / np.tanh(1.2)
    pcm = np.clip(np.round(x * 32767), -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def set_loop(seconds):
    """Loop length for the sounds made next (every periodic part follows it)."""
    global LOOP_S, N, T, FREQS
    LOOP_S = seconds
    N = int(SR * LOOP_S)
    T = np.arange(N) / SR
    FREQS = np.fft.rfftfreq(N, 1 / SR)


def synthesise(tmp):
    """The WAVs, in the add-on's order of random draws: its main() made a 6 s heli_far first and wrote the 12 s one
    over it, so the 6 s one is still made (and dropped) to keep every later sound the same."""
    made = []
    for kind, (near, far) in SOUNDS.items():
        for layer, fn in (("near", near), ("far", far)):
            x = fn()
            if (kind, layer) != ("heli", "far"):
                path = os.path.join(tmp, f"lat_{kind}_{layer}.wav")
                write(path, x)
                made.append(path)
    set_loop(12.0)
    path = os.path.join(tmp, "lat_heli_far.wav")
    write(path, heli_far())
    made.append(path)
    set_loop(16.0)
    path = os.path.join(tmp, "lat_jet_distant.wav")
    write(path, jet_distant())
    made.append(path)
    set_loop(6.0)
    return made


def pcm_of(path):
    with wave.open(path, "rb") as w:
        return (w.getnchannels(), w.getsampwidth(), w.getframerate()), np.frombuffer(w.readframes(w.getnframes()), "<i2")


def compare(made, ref_dir):
    """Each synthesised WAV next to the add-on's (lat_jet_distant: theirs is a recording, not compared)."""
    for path in made:
        name = os.path.basename(path)
        ref = os.path.join(ref_dir, name)
        if name == "lat_jet_distant.wav" or not os.path.exists(ref):
            continue
        with open(path, "rb") as a, open(ref, "rb") as b:
            if a.read() == b.read():
                print(f"  {name}: byte-identical to the add-on's")
                continue
        (fa, xa), (fb, xb) = pcm_of(path), pcm_of(ref)
        if fa != fb or len(xa) != len(xb):
            print(f"  {name}: DIFFERENT format / length: {fa} {len(xa)} vs {fb} {len(xb)}")
            continue
        d = np.abs(xa.astype(np.int32) - xb.astype(np.int32))
        print(f"  {name}: {int(np.count_nonzero(d))} of {len(d)} samples differ, at most by {int(d.max())} LSB")


def ogg(src, dst):
    """Mono Ogg Vorbis, quality 4; bitexact (no random stream serial, no encoder version tag: the same bytes every
    run)."""
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-map_metadata", "-1",
                    "-ac", "1", "-c:a", "libvorbis", "-q:a", "4", "-fflags", "+bitexact", "-flags:a", "+bitexact",
                    dst], check=True, stdin=subprocess.DEVNULL)


def decoded(path, rate):
    """An audio file decoded to mono 16-bit samples at rate (ffmpeg)."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path, "-f", "s16le",
                        "-ac", "1", "-ar", str(rate), "-"], check=True, capture_output=True, stdin=subprocess.DEVNULL)
    return np.frombuffer(r.stdout, "<i2").astype(np.float64)


def seam_report(wav, oggp):
    """The loop seam after encoding: the length kept, the jump from the last sample to the first next to the
    typical sample-to-sample step, the coding error near the ends next to the middle's."""
    (_, _, rate), x = pcm_of(wav)
    x = x.astype(np.float64)
    y = decoded(oggp, rate)
    step = np.percentile(np.abs(np.diff(y)), 99)
    seam = abs(y[0] - y[-1]) if len(y) else float("nan")
    k = min(len(x), len(y))
    e = np.abs(x[:k] - y[:k])
    w = rate // 100                                 # 10 ms
    ends = max(e[:w].max(), e[k - w:k].max())
    mid = e[k // 2 - w:k // 2 + w].max()
    print(f"  {os.path.basename(oggp)}: {len(y)} samples ({len(x)} in), seam step {seam:.0f} (99th pct step "
          f"{step:.0f}), coding error at the ends {ends:.0f} / middle {mid:.0f} LSB")
    return len(y) == len(x)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", default=SRC, help="the add-on's sound folder (lat_jet_close.wav)")
    ap.add_argument("--compare", default=None, help="a folder of the add-on's WAVs to check the synthesis against "
                                                    "(default: --src)")
    args = ap.parse_args()
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found")
    os.makedirs(OUT, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        made = synthesise(tmp)
        print("synthesis vs the add-on's WAVs:")
        compare(made, args.compare or args.src)
        close = os.path.join(tmp, "lat_jet_close.wav")
        shutil.copyfile(os.path.join(args.src, "lat_jet_close.wav"), close)
        made.append(close)
        print("encoded:")
        ok = True
        for wav in made:
            dst = os.path.join(OUT, os.path.basename(wav)[:-4] + ".ogg")
            ogg(wav, dst)
            ok &= seam_report(wav, dst)
            print(f"    {os.path.relpath(dst, ROOT)}  {os.path.getsize(wav) // 1024} KB wav -> "
                  f"{os.path.getsize(dst) // 1024} KB ogg")
    total = sum(os.path.getsize(os.path.join(OUT, f)) for f in os.listdir(OUT))
    print(f"{len(os.listdir(OUT))} files, {total // 1024} KB in {os.path.relpath(OUT, ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

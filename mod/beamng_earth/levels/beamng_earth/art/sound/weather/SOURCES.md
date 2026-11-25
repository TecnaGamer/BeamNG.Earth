# Weather audio sources (thunder + rain)

All sources are from Freesound.org. The licence was checked on each sound's own page: every page
shows "Creative Commons 0" with a link to https://creativecommons.org/publicdomain/zero/1.0/
(checked 2026-11-25). Freesound originals need a login, so these are the public HQ previews linked
from each sound's page (MP3, about 128 kbps VBR). They were checked with `file` and `ffprobe` (all
MPEG ADTS layer III audio), and their sha256 hashes are recorded. The downloaded files are in
`originals/fsNNNNNN/`. Candidates that were downloaded but not used are in
`originals/_unused_candidates/` (also CC0, licence checked on their pages).

## Processing

Every output is Ogg Vorbis, mono, 44.1 kHz, `-q:a 4` (about 55-90 kbps).

- **Thunder** (one-shots): stereo downmixed to mono as (L+R)/2, 25 Hz high-pass, trimmed, linear
  fade-in, quarter-sine fade-out, peak-normalised to -1 dBFS (decoded peaks are -0.8 to -1.4 dBFS).
  Every variant has the same peak level, so the mod has to turn down far thunder itself.
- **Rain loops**: mono downmix, 30 Hz high-pass, resampled to 44.1 kHz. A 22 s segment is turned
  into a 20 s loop by crossfading its last 2 s into its first 2 s with an equal-power (sin/cos)
  crossfade, so the last sample leads straight into the first. Each loop's integrated loudness is
  set to -20 LUFS, measured over the loop played twice. A -1.5 dBFS peak limiter (2 ms attack,
  60 ms release) is then run over three copies of the loop and only the middle copy is kept. This
  shifts the loop by the limiter's 87-sample lookahead, but it stays seamless because nothing is
  cut. The limiter catches only single raindrop transients (20 to 244 samples per loop, up to
  about 7 dB on the medium loop's biggest drops). The wrap was checked after Vorbis decoding: the
  second difference at the wrap stays inside the file's normal range, with no click.

## Files

| Output | Source page | Direct file | Author | Licence (as on page) | Original | sha256 of original | Processing | Duration | Size |
|---|---|---|---|---|---|---|---|---|---|
| bne_thunder_close1.ogg | https://freesound.org/people/DaniloSFX/sounds/672776/ | https://cdn.freesound.org/previews/672/672776_1357949-hq.mp3 | DaniloSFX | Creative Commons 0 | "Thunder Clap" (672776_1357949-hq.mp3) | 813f57934c49e8c5185be340aca884bfdfdc50d208cb042b89f4fb6f1f5483dd | 0.0-14.0 s, fade-in 10 ms, fade-out 2.5 s; bright close crack with a quiet rain bed about 30 dB below | 14.0 s | 111234 B |
| bne_thunder_close2.ogg | https://freesound.org/people/Dave%20Welsh/sounds/194364/ | https://cdn.freesound.org/previews/194/194364_1985044-hq.mp3 | Dave Welsh | Creative Commons 0 | "Thunder Clap OWB KY 441x16.wav" (194364_1985044-hq.mp3) | eccec72a7155f7002a62d6926393eeb600337efb5c738be7ac9c0c11d6296c0f | 13.0-28.0 s, fade-in 0.3 s, fade-out 4 s; tearing crack, then a bang and a roll; -1.0 dB gain | 15.0 s | 123443 B |
| bne_thunder_mid1.ogg | https://freesound.org/people/BlueDelta/sounds/446753/ | https://cdn.freesound.org/previews/446/446753_1790434-hq.mp3 | BlueDelta | Creative Commons 0 | "Heavy Thunder Strike - no Rain - QUADRO.wav" (446753_1790434-hq.mp3) | 343c8e7a6d24b7f71fd65117cb0120bb01460127cbfdf00668c42a0b69ad9c00 | 0.5-15.5 s, fade-in 0.2 s, fade-out 4.5 s; heavy strike and roll, no rain, recorded without limiting | 15.0 s | 102358 B |
| bne_thunder_mid2.ogg | https://freesound.org/people/TRP/sounds/717890/ | https://cdn.freesound.org/previews/717/717890_97550-hq.mp3 | TRP | Creative Commons 0 | "230823 Thunder, dry distant rolling, R-07 EM272s, Stratford 12pm" (717890_97550-hq.mp3) | 9fef0328de54f0555625235c2a8cc6a76f90f90a8c57df6e9be2f07bda591afe | 129.5-144.5 s, fade-in 1.0 s, fade-out 2.5 s; the loudest dry roll in the file (two peaks) | 15.0 s | 106671 B |
| bne_thunder_far1.ogg | https://freesound.org/people/TRP/sounds/717845/ | https://cdn.freesound.org/previews/717/717845_97550-hq.mp3 | TRP | Creative Commons 0 | "230405 Thunder DRY rolling distant low rumbles, roof, EM272s, Toronto 7am" (717845_97550-hq.mp3) | 7b68a632e50b44ce266fcf02f1c68b984b79623f4676972ca59ae714515da7ca | 0.5-14.5 s, fade-in 0.5 s, fade-out 3.5 s; low distant rumble with almost nothing above 1 kHz | 14.0 s | 97166 B |
| bne_thunder_far2.ogg | https://freesound.org/people/TRP/sounds/717890/ | https://cdn.freesound.org/previews/717/717890_97550-hq.mp3 | TRP | Creative Commons 0 | "230823 Thunder, dry distant rolling, R-07 EM272s, Stratford 12pm" (717890_97550-hq.mp3) | 9fef0328de54f0555625235c2a8cc6a76f90f90a8c57df6e9be2f07bda591afe | 149.5-164.5 s, fade-in 0.5 s, fade-out 4 s; distant dry roll | 15.0 s | 106568 B |
| bne_rain_light.ogg | https://freesound.org/people/jmbphilmes/sounds/200273/ | https://cdn.freesound.org/previews/200/200273_2322692-hq.mp3 | jmbphilmes | Creative Commons 0 | "Rain light 2 (rural)" (200273_2322692-hq.mp3) | ae6cb2d0127c8682e079dbad1b5c0c49716b05ca2d365161d5ac5cb4acfd9d9d | segment 40.0-62.0 s turned into a 20 s loop (2 s crossfade); +32.7 dB to -20.2 LUFS; limiter | 20.0 s loop | 191947 B |
| bne_rain_medium.ogg | https://freesound.org/people/jmbphilmes/sounds/200270/ | https://cdn.freesound.org/previews/200/200270_2322692-hq.mp3 | jmbphilmes | Creative Commons 0 | "Rain heavy 1 (rural)" (200270_2322692-hq.mp3) | 7bb21a259ea30d1336cc6b3b1ea40e2ab65d1142685b0bee43147c67e49cfbb4 | segment 34.0-56.0 s (the take's quieter, early part, -40.7 dBFS RMS) turned into a 20 s loop (2 s crossfade); +19.4 dB to -20.5 LUFS; limiter | 20.0 s loop | 228714 B |
| bne_rain_heavy.ogg | https://freesound.org/people/jmbphilmes/sounds/200270/ | https://cdn.freesound.org/previews/200/200270_2322692-hq.mp3 | jmbphilmes | Creative Commons 0 | "Rain heavy 1 (rural)" (200270_2322692-hq.mp3) | 7bb21a259ea30d1336cc6b3b1ea40e2ab65d1142685b0bee43147c67e49cfbb4 | segment 192.0-214.0 s (the take's peak, -34.2 dBFS RMS, brighter and denser) turned into a 20 s loop (2 s crossfade); +12.5 dB to -20.3 LUFS; limiter | 20.0 s loop | 225606 B |
| bne_rain_roof.ogg | https://freesound.org/people/Nox_Sound/sounds/535870/ | https://cdn.freesound.org/previews/535/535870_9250976-hq.mp3 | Nox_Sound | Creative Commons 0 | "Ambiance_Rain_Inside_Car_Close_Roof_Loop_Stereo.wav" (535870_9250976-hq.mp3) | 2f79e90a4f9ace5e4bd36a2d059e8b201ad214175a18333156cfdd3982c2e0a2 | real recording of rain inside a car, mic close to the roof (Rode NT4); segment 0.5-23.0 s turned into a 20 s loop (2 s crossfade); +10.5 dB to -20.4 LUFS; limiter | 20.0 s loop | 211439 B |

Total size of the outputs: 1,505,146 bytes (about 1.44 MiB).

## Notes for the mod

- The three outdoor rain loops all come from one recordist with one microphone (Sennheiser ME66 on
  a boom pole, Tascam HD-P2, rural Brazil, September 2013), so crossfading between them should not
  change the sound's character. Medium and heavy come from different parts of the same take. Before
  normalising they were about -54, -41 and -34 dBFS RMS, so the real rain was louder at each step.
  The mod should restore part of that difference with its volume curve. They now all sit at about
  -20 LUFS.
- Thunder "far" variants are peak-normalised like the close ones; play them quieter (and/or
  later after the flash) by distance.
- bne_rain_roof is a real interior recording at medium intensity. There is no separate heavy roof
  loop. If one is needed, use bne_rain_roof at a higher volume, or low-pass bne_rain_heavy
  (about 1 kHz), which would then be a derived file.

## Credit lines (for CREDITS.md; CC0 needs no credit, given anyway)

- Thunder clap: "Thunder Clap" by DaniloSFX, https://freesound.org/s/672776/ (CC0)
- Thunder clap: "Thunder Clap OWB KY 441x16.wav" by Dave Welsh, https://freesound.org/s/194364/ (CC0)
- Thunder strike: "Heavy Thunder Strike - no Rain - QUADRO.wav" by BlueDelta, https://freesound.org/s/446753/ (CC0)
- Distant thunder: "230405 Thunder DRY rolling distant low rumbles" and "230823 Thunder, dry distant rolling" by TRP, https://freesound.org/s/717845/ and https://freesound.org/s/717890/ (CC0)
- Rain: "Rain light 2 (rural)" and "Rain heavy 1 (rural)" by jmbphilmes, https://freesound.org/s/200273/ and https://freesound.org/s/200270/ (CC0)
- Rain in car: "Ambiance_Rain_Inside_Car_Close_Roof_Loop_Stereo.wav" by Nox_Sound, https://freesound.org/s/535870/ (CC0)

## Derived in the mod

- bne_rain_muffled.ogg: bne_rain_medium.ogg (jmbphilmes, CC0) through two 900 Hz low-pass filters and +4 dB
  (`ffmpeg -af "lowpass=f=900,lowpass=f=900,volume=1.6" -c:a libvorbis -q:a 3 -ac 1`): the rain heard from under a
  roof or deck (ambience.lua RAIN_MUFFLED). 20 s loop, 97,920 bytes.

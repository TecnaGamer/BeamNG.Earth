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
| bne_thunder_mid2.ogg | https://freesound.org/people/Gerent/sounds/558748/ | https://cdn.freesound.org/previews/558/558748_556505-hq.mp3 | Gerent | Creative Commons 0 | "thunder-rolling-170315_0057 035-068 032-074.wav" (558748_556505-hq.mp3) | 4219f4a28f6fb04173aefc00ab945139319809b6e05a8463a518fe359ffc948b | revised 2026-11-25 (the user asked for another roll): 4.0-19.0 s, mono, 25 Hz high-pass, fade-in 0.3 s, fade-out 3.5 s, loudest 3 s to -18 LUFS (+0.5 dB), -1 dBFS true-peak limiter; a clean rolling peal a few km off, no crack, no rain (the uploader: noise reduction applied) | 15.0 s | 121984 |
| bne_thunder_far1.ogg | https://freesound.org/people/TRP/sounds/717845/ | https://cdn.freesound.org/previews/717/717845_97550-hq.mp3 | TRP | Creative Commons 0 | "230405 Thunder DRY rolling distant low rumbles, roof, EM272s, Toronto 7am" (717845_97550-hq.mp3) | 7b68a632e50b44ce266fcf02f1c68b984b79623f4676972ca59ae714515da7ca | 0.5-14.5 s, fade-in 0.5 s, fade-out 3.5 s; low distant rumble with almost nothing above 1 kHz | 14.0 s | 97166 B |
| bne_thunder_far2.ogg | https://freesound.org/people/TRP/sounds/717890/ | https://cdn.freesound.org/previews/717/717890_97550-hq.mp3 | TRP | Creative Commons 0 | "230823 Thunder, dry distant rolling, R-07 EM272s, Stratford 12pm" (717890_97550-hq.mp3) | 9fef0328de54f0555625235c2a8cc6a76f90f90a8c57df6e9be2f07bda591afe | 149.5-164.5 s, fade-in 0.5 s, fade-out 4 s; distant dry roll | 15.0 s | 106568 B |
| bne_rain_light.ogg | https://freesound.org/people/jmbphilmes/sounds/200271/ | https://cdn.freesound.org/previews/200/200271_2322692-hq.mp3 | jmbphilmes | Creative Commons 0 | "Rain light 1 (rural)" (200271_2322692-hq.mp3) | 9150f775fea3c4aa020edff9174fb0d1a1d4c5cfe464a0ce6f3dd414065005be | revised 2026-11-25 (see "Revision" below): segment 20.0-42.0 s, 30 Hz high-pass, turned into a 20 s loop (2 s equal-power crossfade), +25.4 dB to -20.2 LUFS, no limiting needed (peak -3.3 dBFS) | 20.0 s loop | 188190 |
| bne_rain_medium.ogg | https://freesound.org/people/jmbphilmes/sounds/200270/ | https://cdn.freesound.org/previews/200/200270_2322692-hq.mp3 | jmbphilmes | Creative Commons 0 | "Rain heavy 1 (rural)" (200270_2322692-hq.mp3) | 7bb21a259ea30d1336cc6b3b1ea40e2ab65d1142685b0bee43147c67e49cfbb4 | segment 34.0-56.0 s (the take's quieter, early part, -40.7 dBFS RMS) turned into a 20 s loop (2 s crossfade); +19.4 dB to -20.5 LUFS; limiter | 20.0 s loop | 228714 B |
| bne_rain_heavy.ogg | https://freesound.org/people/jmbphilmes/sounds/200270/ | https://cdn.freesound.org/previews/200/200270_2322692-hq.mp3 | jmbphilmes | Creative Commons 0 | "Rain heavy 1 (rural)" (200270_2322692-hq.mp3) | 7bb21a259ea30d1336cc6b3b1ea40e2ab65d1142685b0bee43147c67e49cfbb4 | segment 192.0-214.0 s (the take's peak, -34.2 dBFS RMS, brighter and denser) turned into a 20 s loop (2 s crossfade); +12.5 dB to -20.3 LUFS; limiter | 20.0 s loop | 225606 B |
| bne_rain_roof.ogg | https://freesound.org/people/Nox_Sound/sounds/535870/ | https://cdn.freesound.org/previews/535/535870_9250976-hq.mp3 | Nox_Sound | Creative Commons 0 | "Ambiance_Rain_Inside_Car_Close_Roof_Loop_Stereo.wav" (535870_9250976-hq.mp3) | 2f79e90a4f9ace5e4bd36a2d059e8b201ad214175a18333156cfdd3982c2e0a2 | real recording of rain inside a car, mic close to the roof (Rode NT4); segment 0.5-23.0 s turned into a 20 s loop (2 s crossfade); +10.5 dB to -20.4 LUFS; limiter | 20.0 s loop | 211439 B |
| bne_thunder_strike1.ogg | https://freesound.org/people/Simon%20Spiers/sounds/437243/ | https://cdn.freesound.org/previews/437/437243_213249-hq.mp3 | Simon Spiers | Creative Commons 0 | "Very close lightning strike." (437243_213249-hq.mp3; the uploader: a cloud-to-ground strike within 100 m) | 9e4169956c8ff03d204923cd4e8ef6c036c6724af92c269ef42e484ee583dbe7 | added 2026-11-25 (the 'super close' class): 20.80-34.80 s (the crack at the start), mono, 20 Hz high-pass (sub-bass kept), fade-in 5 ms, fade-out 4 s, +22 dB into a limiter (5 ms attack, 200 ms release, 4x oversampled), loudest 3 s -11.7 LUFS, true peak -0.4 dBFS, Vorbis q5 | 14.0 s | 136270 |
| bne_thunder_strike2.ogg | https://freesound.org/people/danielmcadams/sounds/483684/ | https://cdn.freesound.org/previews/483/483684_7588344-hq.mp3 | danielmcadams | Creative Commons 0 | "Zoom H4n - Super Close Lightning Strike and Thunderstorm" (483684_7588344-hq.mp3; the uploader: a bolt hit just across the street) | 39a198e33d2ff05172d0b86691a9656a0e6a02cff7589832e310f5399648223b | added 2026-11-25: 19.10-33.10 s (the strike's crack at the start; the storm's rain bed under it), as strike1 with +22 dB, loudest 3 s -10.1 LUFS, true peak -0.3 dBFS | 14.0 s | 144729 |

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

## Revision (2026-11-25, after the user listened)

- **bne_rain_light.ogg** replaced: the first version (jmbphilmes "Rain light 2 (rural)", freesound 200273, CC0) is a
  very quiet take (-54 dBFS RMS) that needed +33 dB, and close drops on something nearby sounded like rain on a tarp or
  a roof. Now the same recordist's "Rain light 1 (rural)" (freesound 200271, licence "Creative Commons 0" checked on its
  page 2026-11-25; same microphone, place and day as the medium and heavy loops), a steady 20-42 s stretch (-46 dBFS RMS,
  crest factor ~7, no near transients). Row above.
- **Thunder by loudness per class** (the first versions were all peak-normalised, so the dry distant roll mid2 sounded
  quieter than the far rumbles): each file cut again from its original as in the table (same times, fades, 25 Hz
  high-pass, mono), its loudest 3 s (EBU R128 short-term) set to the class's level, close -14, mid -18, far -22 LUFS,
  then a -1 dBFS limiter (2 ms attack, 80 ms release), Ogg Vorbis q4. Gains: close1 +1.9, close2 -1.4, mid1 -0.2, mid2
  -0.3, far1 -0.3, far2 +1.4 dB (relative to the cut, before the limiter). The game still scales each thunder by its
  distance (storm.lua thunderFor). Sizes now: close1 115,725, close2 125,014, mid1 103,937, mid2 107,854, far1 99,610,
  far2 108,004 bytes. The first versions are kept outside the repo (~/bnge-tests/weather/b/audio/v1).

- **bne_thunder_mid2.ogg** replaced (the user wanted a different roll with more body, not the same one louder): Gerent's
  "thunder-rolling" (freesound 558748, "Creative Commons 0" on its page, checked 2026-11-25). Two other candidates were
  made the same way for the user to compare, outside the repo (~/bnge-tests/weather/b/audio/candidates): sarson
  "Distant Thunder.wav" (freesound 195522, CC0, sha256 8ed08374445e220b14f32b671cc4fa7814bdc261d1ba2ec7991016c78116d179,
  4.0-19.0 s) and rockyistanbul "Thunder rolls.mp3" (freesound 640249, CC0, sha256
  ee9f566c3ed4376b5bbb65933661d8a953b378214d12521eb122dc5982ab037c, 33.0-48.0 s).
- **bne_thunder_strike1/2.ogg** added: the "super close" class (storm.lua STRIKE_M: strikes within 300 m, no delay
  after the flash, full volume, the rain bed ducked). Recordings made by a strike (rows above). The user's target was -9
  to -10 LUFS on the loudest 3 s: these recordings are one huge transient each, so they need ~22 dB into the limiter to
  get there; strike2 reaches -10.1, strike1 stops at -11.7 (more would flatten its crack). Two more candidates for the
  user, outside the repo (~/bnge-tests/weather/b/audio/candidates): csengeri "Close lightning strike 2018 07 07"
  (freesound 434359, CC0, sha256 1eb7ddad74d711bdc337e848be80683c6650f19e92f28a3ce69c265236012d02, 2.0-16.0 s, -9.7
  LUFS) and TRP "210914 Thunder, crack, pretty close" (freesound 616992, CC0, sha256
  05c32815b4919e0b6557a720594bdc8bd36a882e2157bc683ba57a36e4827008, 0.98-14.98 s, -9.3 LUFS).
- Every new original was checked with `file` and `ffprobe` (MPEG audio, nothing else) and its sha256 recorded.

## Derived in the mod

- bne_rain_muffled.ogg: bne_rain_medium.ogg (jmbphilmes, CC0) through two 900 Hz low-pass filters and +4 dB
  (`ffmpeg -af "lowpass=f=900,lowpass=f=900,volume=1.6" -c:a libvorbis -q:a 3 -ac 1`): the rain heard from under a
  roof or deck (ambience.lua RAIN_MUFFLED). 20 s loop, 97,920 bytes.

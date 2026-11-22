# Custom BeatSaver songs on PS5

`custom_songs.py` turns BeatSaver map downloads into a new **Custom Songs** pack for the PS5 version, and
delivers it as a small overlay. You don't rebuild or reinstall the game package: KrakenMount lays the
overlay's files over the installed package when the game starts. Each map becomes a new song, so no
official song is replaced.

## What you need

| | |
|---|---|
| Installed package | The exact `.pkg` that is installed on the console, e.g. `Beat Saber (v01.039.000) Modded.pkg`. The overlay's catalog is built from **its** catalog, so a different package gives a wrong overlay. |
| Songs | BeatSaver downloads: the `.zip` files as downloaded (e.g. `2f284 (Night Dancer - seasonsmouse & Tuc).zip`) or unzipped map folders. Keep BeatSaver's download name, `<key> (<title> - <mapper>)`: the key becomes the song's ID (`BeatSaver<key>`). A map with any other name gets an ID made from its Info.dat. |
| ffmpeg, soundfile | ffmpeg decodes the audio (`brew install ffmpeg`); the `soundfile` package (in `requirements.txt`) encodes the Vorbis. |
| PS5 Dump Forge, Python | As for `port.py` (see the README). |

## Build

Put the maps in a folder, e.g. `songs/` (ignored by git), and run:

```bash
.venv/bin/python custom_songs.py --pkg "/path/Beat Saber (v01.039.000) Modded.pkg" --songs songs --out work/custom_overlay
```

The first run extracts the package into `work/installed/` (about 5.5 GB), and later runs reuse it.
`--out` must be an empty or new folder. It receives only the files that change:

```
Media/StreamingAssets/aa/catalog.json                      the installed catalog plus the new pack
Media/StreamingAssets/aa/PS5/customsongs_pack_assets_all_<hash>.bundle    the pack, all songs
Media/StreamingAssets/BeatmapLevelsData/beatsaver<key>    one file per song
```

The run ends with an offline check of everything it wrote. If anything is wrong it prints `FAIL` lines and
exits with an error, and nothing should be uploaded. Each song's notes are printed, such as skipped
difficulties and replaced environments. The full report is in `work/custom_overlay.report.json`.

### Carrying ported packs along

To ship packs that `port.py` built but the installed package doesn't have yet, add `--with work/overlay`.
Every file under its `Media/StreamingAssets` that the installed package lacks or has different is copied into
the output, and its catalog becomes the base (one `--with` folder per run). For example, a package built before the porter added OST Vol. 8, OST Vol. 9 and the updated
Extras pack gets those 17 songs (about 290 MB) this way. A package that already has them gets nothing extra. Only use the `work/overlay` of the same `port.py` run family as the installed package.

## Install on the PS5

1. Make sure KrakenMount's backport overlay is on (`backports = true` in `/data/krakenmount/config.toml`).
2. Upload the **contents** of the output folder to `backports/PPSA15035/` **in the folder that holds the
   package**. KrakenMount applies a game's backports from its own scan folder first. For a package in
   `/mnt/ext1/etaHEN/games/` that is `/mnt/ext1/etaHEN/games/backports/PPSA15035/`, and afterwards
   `/mnt/ext1/etaHEN/games/backports/PPSA15035/Media/StreamingAssets/aa/catalog.json` must exist.
   Upload files only, no empty folders. A folder in the backport area replaces the game's own folder of the
   same name (an empty `Media/UnitySubsystems` once hid the PS VR2 files and crashed the game).
3. Launch Beat Saber. KrakenMount notices the changed backport and rebuilds its cache. The songs are under
   Music Packs → **Custom Songs**, after the official packs.

To remove everything, delete that `backports/PPSA15035` folder. The installed package is never
changed.

## Adding or removing songs later

Put every song you want in the folder and run the build again into a new, empty `--out`. The pack always
holds the whole set. On the console, delete the old `backports/PPSA15035` folder and upload the new output.
(Unchanged songs produce byte-identical files, which a future sync step can use to skip them.)

If the installed package changes (a rebuilt DLC package, a game update), rebuild the overlay from the new
package. The overlay's catalog replaces the package's, so an overlay built for an old package hides what
the new one adds.

## What works

| Map content | Handling |
|---|---|
| Info.dat 2.0, 2.1, 4.x | Read. 2.1 and 4.x environment lists and colour schemes are kept per difficulty. |
| Charts 2.x, 3.x, 4.x up to `max_beatmap_version` in `config.json` (4.1.0) | Stored as they are (a UTF-8 byte-order mark is removed). The game picks its v2, v3 or v4 loader from the chart's `_version`/`version`. v4 lightshows and audio data are included; the audio data's sample positions are rescaled to the 44.1 kHz audio. |
| Characteristics | Standard, One Saber, No Arrows, 360°, 90°, Legacy. Others (Lightshow, Lawless) are skipped. |
| Environments | A PS5 environment is kept. A PC-only one becomes its `environment_fallbacks` entry in `config.json`, else `default_environment`. 360°/90° maps use an all-directions environment (Glass Desert, or an optional `rotation_environment` key you add to `config.json`). |
| Mod maps | A difficulty that *requires* mods (Noodle Extensions, Mapping Extensions, Chroma as a requirement) is skipped. A song whose difficulties all need mods is left out of the build with a note; so is any map the tool can't use (missing or unreadable audio, cover or Info.dat), and the other songs are still built. A map's unusable v4 audio data falls back to the Info.dat BPM, with a note. Suggested mods (e.g. Chroma) are ignored: the map plays with the base game's lights. |
| Covers | Cropped square, stored as linear RGBA32 like the ported packs. The pack cover is the first song's cover, or a 2×2 mosaic from four songs. |
| Credits | The pack's credits page lists every song: title, artist, mappers, and the BeatSaver key when the download name has one. It grows with the song count. |
| Online | No leaderboard scores: the songs have no PS5 leaderboard ID, and the game skips those (`TryGetSonyLeaderboardId`). Scores are kept locally, keyed by `BeatSaver<key>`. |

## Audio

The map's audio (`.egg`/`.ogg`) is decoded with ffmpeg to 16-bit 44.1 kHz stereo and stored in FMOD's FSB5
container, the way the game's own songs are. Choose the format with `--audio`:

| `--audio` | What it is | Size | Status |
|---|---|---|---|
| `vorbis` (default) | Vorbis, like the stock songs (FSB5 mode 15, AudioClip compressed in memory). | about 3 MB per minute | Checked offline (below); **not yet played on the console**. |
| `pcm` | Uncompressed PCM16 (FSB5 mode 2, decompressed on load), like the game's 36 stock PCM sound effects. | about 10 MB per minute | Played on the console. |

Why the Vorbis works: FMOD does not store a Vorbis setup header in the file. It keeps a table of the headers
its own encoder makes and picks one by the CRC32 written in the FSB5's VORBISDATA chunk. libvorbis at its
highest quality (libsndfile `compression_level` 0.0, through the `soundfile` package) makes exactly the header
the stock songs name: CRC `6D39BF3E`, as in `BeatmapLevelsData/houdini`. If a different libsndfile/libvorbis
build ever makes another header, the build stops and says to use `--audio pcm`. Checked offline:

- the stock Houdini audio decodes without a single error using this encoder's setup header (with a different
  header it fails), so the two are the same header;
- the tool's seek table (the last packet before every whole second, as sample position and byte offset,
  computed from the Vorbis block sizes) reproduces the stock file's table exactly;
- every built clip is decoded back in full as part of the build (no decoder errors, the right number of
  samples) and its seek table is recomputed and compared;
- measured once on Night Dancer: the same length as the source, no time shift, about 40 dB signal-to-noise
  (normal for high-quality Vorbis).

BeatSaver audio is already Vorbis, so `vorbis` is a second lossy encode; at about 350–400 kbps (the stock
songs are about 460) that is not audible. `pcm` adds no loss of its own. Both convert 48 kHz songs to 44.1 kHz.

Previews are cut from the map's preview start and length, with a short fade, in the same format.

## Checked in the game code (PS5 1.39)

| Behaviour | Where |
|---|---|
| Song file path and asset name | `BeatmapLevelLoader.LoadBeatmapLevelDataFromAssetBundle`: `BeatmapLevelsData/<levelid lower-case>`, asset `<levelID>BeatmapLevelData.asset`. |
| Chart format choice | `BeatmapDataLoader.LoadBeatmapData`: `BeatmapSaveDataHelpers.GetVersion` reads `_version`, else `version`. Below 3.0.0 goes to the v2 loader, below 4.0.0 to v3, otherwise v4. |
| No lightshow asset | `DifficultyBeatmap.GetLightshowString` returns null when the asset is missing (v2/v3 charts carry their own lights). |
| Credits needed | `PackDefinitionSO.hasCredits` is true for any pack with tags 1–3, so the pack ships a real credits prefab. |
| Ownership | `_tags` 3, like the ported packs: listed under Music Packs, always owned (see HOW-IT-WORKS.md). |
| Characteristics, environments | Read from the PS5 build itself: `sharedassets` (BeatmapCharacteristicSO) and the environment bundles. |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| No Custom Songs pack | The overlay wasn't applied. Check `backports = true`, the path (`.../backports/PPSA15035/Media/...`), and that the KrakenMount log says the cache was rebuilt. |
| Game crashes at start | Delete `backports/PPSA15035` and check the game starts. Then make sure no empty folders were uploaded, and that the overlay was built from the installed package. |
| A song is missing a difficulty | It needs mods, uses a characteristic the PS5 game lacks, its chart file is missing or unreadable, or its chart version is newer than `max_beatmap_version`. The build notes say which. |
| A song is missing | The map was left out: the `left out:` lines of the build (and `left_out` in the report) say why. |
| "the installed catalog already has the custom pack" | `--pkg`/`--base` points at a game that already contains an overlay. Build from the installed package itself. |

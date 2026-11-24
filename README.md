# Beat Saber PS5 DLC porter

Ports Beat Saber music packs that exist on PC but not on PS5 into the PS5 version, as **new** packs and
songs next to the existing ones, and packs the result as a self-contained PS5 fake package (debug FPKG).
Optionally it makes every DLC pack always owned, so no `dlc_emu.ini` is needed.

First port (PS5 Beat Saber 1.39.0, PPSA15035): Coldplay (12 songs), The Prodigy (6), Hip Hop Mixtape 2 (8)
and 9 extra Shock Drops, verified in the headset. Against PC 1.40+ the default run also finds OST Vol. 8,
OST Vol. 9 and an updated Extras pack. These pass every offline check but haven't been tested in the headset;
list them in `skip_packs` to leave them out.

`skip_packs` takes PC bundle names without the hash and extension, e.g. `"ostvol8_pack_assets_all"`, as printed
in the plan at the start of a run. An entry that matches no PC pack is reported in the report's `warnings`.

How and why each step works, with the game-code evidence: [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md).

Custom BeatSaver songs go in a separate **Custom Songs** pack, delivered as a KrakenMount overlay on top of
the installed package (no rebuild): [docs/CUSTOM-SONGS.md](docs/CUSTOM-SONGS.md).

## What you need

| | |
|---|---|
| PS5 game | The **original, unmodified** Beat Saber PS5 fake package (plaintext debug FPKG, e.g. `Beat Saber (v01.039.000).pkg`), or a folder extracted from it. Never a modded package: every run builds a complete new mod from the original. **Keep a copy.** |
| PC game | A PC Beat Saber install with the DLC you want **downloaded**: the folder holding `Beat Saber_Data` and `DLC/Levels`. |
| PS5 Dump Forge | The `ps5-dump-forge` command-line tool (0.5.6 was used), not included here. Put it in `bin/` or point `dump_forge` in `config.json` at it. |
| Python | 3.9 or newer. |

The PC game must be the version whose content you want. It can be newer than the PS5 build: Unity 6
PC content was ported to the PS5's Unity 2022.3 player.

## Setup

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python port.py --ps5-pkg "/path/Beat Saber (v01.039.000).pkg" --pc-game "/path/Beat Saber" --work work
```

Use `--ps5-dir work/ps5_original` instead of `--ps5-pkg` once the original has been extracted (the first
run with `--ps5-pkg` extracts it there). Add `--no-pkg` to stop after validation.

The run:

1. Extracts the original PS5 package into `work/ps5_original` (once).
2. Compares the packs under the `PackDefinition` label of both catalogs, matched by pack ID, and picks every
   PC pack that is **new** (no PS5 pack with that ID) or **updated** (the PC copy has every PS5 level plus
   more, like Shock Drop; see "Updated packs" below).
3. Converts those pack bundles and their song files for the PS5 player and applies the PS5 fixes.
4. Makes every DLC pack always owned, if `unlock_all_dlc` is on (the default).
5. Merges the new entries into the original PS5 catalog.
6. Validates everything offline (`validate.py`): any `FAIL` line stops the run.
7. Assembles `work/game` (original + changes; `dlc_emu.ini` left out when `unlock_all_dlc` is on) and packs
   `work/Beat Saber (v<version>) Modded.pkg` with full verification.

What it did is in `work/report.json`. Song covers are drawn in `work/covers.png` for a quick look.

### Updated packs

An updated pack's PC bundle replaces the PS5 one, and the PC bundle has no PS5 store or leaderboard data.
The levels the PS5 pack already had keep working but no longer post online leaderboard scores. With
`unlock_all_dlc` off they would lock, so the porter then leaves a paid pack un-updated and says so in the
report's `warnings`.

## Install on the PS5

The console used KrakenMount, which serves games from packages in its scan folders (e.g.
`/data/etaHEN/games`).

1. Remove any older Beat Saber package or folder from the scan folders, and any stray
   `backports/PPSA15035` folder (a backport folder overlays its files on the game; empty folders in it
   hide the game's own).
2. Copy the new `.pkg` into a scan folder, wait for KrakenMount to register it, then launch Beat Saber.

**Run it as a package, not as an extracted folder.** As a folder, even the unmodified game crashes at
start-up (error CE-108255-1, a few seconds after launch, inside Sony's PSVR2 plugin), before any mod
content loads.

## When new PC DLC comes out

1. Update the PC game and download the new DLC (it lands in `DLC/Levels`).
2. Run `port.py` again with the same original PS5 game. The plan it prints lists the packs it found.
3. Read `work/report.json`, especially `environments`: a PC-only environment is replaced by an
   existing PS5 one. Add a specific choice to `environment_fallbacks` in `config.json` if the default
   (`default_environment`) doesn't suit the pack.
4. Read `warnings` in the report: packs left out, and updated packs that lose leaderboards.
5. Install and test the package. Check: new packs under Music Packs, song covers, a song plays, DLC unlocked.

A pack is ported only if all of these hold. The run stops with a message otherwise:

- every script it uses exists in the PS5 build
- every bundle it depends on exists on PS5 (an environment bundle is never needed: environments are remapped)
- every missing cross-bundle reference is a cover sprite the PC shared bundle has
- every song's data is in the PC game, in a beatmap format no newer than `max_beatmap_version`

Always start from the **original** PS5 package, not the previous modded one. Each run produces a complete
modded package with everything the PC game has that the original lacks (earlier ports included), so the new
package simply replaces the old one on the console.

## When the PS5 game updates

The porter relies on several behaviours of the PS5 game code (how packs are found, how ownership is
decided, where song files live, CRC checks). Use the new version's original package, run `port.py`
with `--no-pkg` once so it extracts it into `work/ps5_original`, then re-check those behaviours:

```bash
.venv/bin/python analysis/check_game.py work/ps5_original
```

Pass your config file as a second argument if it isn't `config.json`.

All lines `OK`: port as usual. A `CHANGED` line: re-verify that behaviour with the method in
docs/HOW-IT-WORKS.md before trusting a build.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| CE-108255-1 a few seconds after launch | Game run as a folder, or a stray `backports/PPSA15035` folder. Run it as a package; remove the backport folder. |
| New songs show a lock or a store button | `unlock_all_dlc` was off: DLC packs without PS5 store data count as not owned. |
| A pack is missing from Music Packs | Its bundle failed to load. Changed bundles need `m_Crc` 0 in the catalog (the porter does this); check the validator output. |
| A cover is blank or garbled | Block-compressed textures are GPU-tiled on PS5. The porter re-encodes covers as linear RGBA32; check `covers.png`. |
| `port.py` stops with "script … does not exist in the PS5 build" | That pack needs game code the PS5 version doesn't have; it can't be ported as data. |

## Files

| File | Purpose |
|---|---|
| `port.py` | The pipeline. |
| `custom_songs.py` | BeatSaver maps → Custom Songs pack overlay ([docs/CUSTOM-SONGS.md](docs/CUSTOM-SONGS.md)). |
| `validate.py` | Offline checks of a build (run by `port.py`). |
| `catalog_io.py` | Reads and writes Unity Addressables `catalog.json` (byte-exact round trip). |
| `unity_bundles.py` | Bundle helpers: PS5 retargeting, PS5 file layout, linear textures. |
| `config.json` | Environment fallbacks, DLC unlock, packs to skip, newest beatmap format the PS5 build reads, Dump Forge path. |
| `analysis/` | Readers for the decrypted game code: SELF unwrapping, IL2CPP metadata, annotated disassembly. `check_game.py` uses them. |

https://ko-fi.com/mtp24

https://buymeacoffee.com/mtp24

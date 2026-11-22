# How the port works

Everything here was checked in the decrypted PS5 game code (Beat Saber 1.39.0, PPSA15035, Unity
2022.3.33f1, IL2CPP) and then confirmed on the console. Addresses are virtual addresses inside the
unwrapped `Media/Modules/Il2cppUserAssemblies.prx` of that version. They change with every game build;
`analysis/check_game.py` re-finds the behaviours by name.

## 1. Reading the game code

- The package is a fake-signed debug FPKG: its PFS is plaintext, so PS5 Dump Forge extracts it.
- `eboot.bin` is only the Unity player. All game code is IL2CPP in `Media/Modules/Il2cppUserAssemblies.prx`,
  a fake SELF whose segments are plaintext. `analysis/unself.py` turns it into a plain ELF.
- `Media/Metadata/global-metadata.dat` (IL2CPP metadata v31, unencrypted) gives type, field and method names
  and string literals (`analysis/meta.py`).
- `analysis/il2.py` maps every method to its address: it finds each assembly's code-gen module through the
  ELF's RELATIVE relocations (the module name string → the module struct → its method-pointer array, indexed
  by method token). It also decodes IL2CPP's lazily-initialised metadata globals ((type << 29) | (index << 1)
  | 1), so the disassembly shows string literals, types and called methods by name.
- `analysis/xref.py` finds the code that references an address (e.g. a string literal).

## 2. How the game finds and owns packs

| Behaviour | Where (1.39) | Consequence |
|---|---|---|
| Pack definitions are loaded by the Addressables **label `PackDefinition`** | `PackDefinitionAsyncInstaller.LoadPackDefinitionListAsync` | A new pack needs only catalog entries: no code change. |
| `PackDefinitionSO._tags`: bit 0 built-in/OST, bit 1 DLC, bit 2 in development | getters `get_isOST` 0x806500, `get_isDLC` 0x806510, `get_isEntitlementCheckSkipped` 0x806520 (`(tags & 3) != 2`) | |
| `LoadAllBeatmapLevelPacks` 0x805700: `(tags&3)==1` → OST list; `tags&2` → DLC list; `(tags&3)!=2` → `AddAlwaysOwnedPack` | the only code that reads `_tags` (all getters are inlined; no interface exposes them) | `_tags = 3` puts a pack in Music Packs **and** makes it always owned. |
| Level ownership: always-owned set first, else the Sony model | `BeatmapLevelsEntitlementModel.GetLevelEntitlementStatusAsync` 0x804e50 | |
| Sony model with no PS5 product data for a level: logs "Product data not found" and returns **not owned** | `SonyPlatformAdditionalContentModel.HasLevelEntitlement` 0x832c10 | PC packs carry no PS5 store data, so `_tags = 2` would lock them. |
| Null PS5 product/leaderboard data is skipped | `PackDefinitionExtensions` iterators (`Object.op_Inequality`), `TryGetSonyLeaderboardId` in score upload/download | PC packs load safely; they just have no online leaderboards. |
| A failed entitlement query stores an empty set and returns normally | `SonyPlatformAdditionalContentModel.UpdateEntitlementsAsync` | Without `dlc_emu.ini` nothing breaks; owned-ness comes from `_tags`. |

The game never mounts add-on content: `dlc_emu`'s only job for this title is answering the entitlement
query. That's why `unlock_all_dlc` (every DLC pack `_tags` 2 → 3) removes the need for the ini.

## 3. Song files

- Path: `StreamingAssets/BeatmapLevelsData/<levelID lower-case>` (`BeatmapDataAssetsModel.GetAssetBundleStreamingAssetsPathForBeatmapLevelId`
  = `Path.Combine(.., levelId.ToLower())`). The versioned name `"{0}.{1}"` exists but has no callers on PS5.
- Loaded with the one-argument `AssetBundle.LoadFromFileAsync(path)`: no CRC, and no catalog entry is needed.
  The asset is `<levelID>BeatmapLevelData.asset`.
- PC beatmaps are format 4.1.0, PS5 ones 4.0.0. The loader dispatches with `System.Version` thresholds, and
  the PS5 build's own `BeatmapSaveDataVersion4.BeatmapSaveData` writes 4.1.0, so both load.
- PC song bundles are structurally identical (same level-data script, Vorbis audio). They need only retargeting (§5).

## 4. The Addressables catalog

`catalog.json` stores keys, buckets, entries and extra data as base64 binary (`catalog_io.py`, byte-exact
round trip).

- **Never build it from the PC catalog.** The PC catalog has 2 providers and 89 resource types; the PS5 one
  has 4 and 111, and entries refer to both by index. A PC-derived catalog crashes the game.
- A new pack adds one bundle entry (AssetBundleProvider, options JSON), one dependency key (a numeric
  **string** key) whose bucket is `[pack bundle] + the 8 shared bundles every PS5 pack uses`, plus the bundle of
  every other PS5 CAB the converted bundle refers to. It also adds its asset entries (pack definition, promo
  info, credits prefab) with the PC catalog's keys: name, GUID, and labels `PackDefinition` / `MusicPackCredits`.
- Packs are matched across the two catalogs by pack ID (`BeatmapLevelPackSO._packID`), not by bundle file name.
- **CRC**: `AssetBundleResource.LoadLocalBundle` passes `m_Crc` to `LoadFromFileAsync`. Every bundle that
  differs from the original gets `m_Crc` 0 (no check) and its real size. A stale CRC makes the bundle fail to
  load, and with it the whole pack list (all packs load in one label call).

## 5. Converting PC bundles

PC 1.40+ is built with Unity 6 (6000.x); PS5 1.39 with 2022.3.33f1. All bundles carry type trees, so the
PS5 runtime reads objects field by field (missing fields default, unknown ones are skipped). Built-in type
trees are identical except Texture2D (PS5 has 2 extra fields) and Sprite (PC has `m_ScriptableObjects`).

Per bundle (`unity_bundles.load_as_ps5`, `save_ps5`):

- engine version strings → the PS5 build's version; target platform → 44 (PS5; PC is 19 or 5)
- written in the stock PS5 layout: flags 0x243 (LZ4HC, block info at the start, padding)

Per pack bundle (`port.fix_pack_bundle`):

| Fix | Why |
|---|---|
| MonoScript assembly name corrected to the PS5 one (e.g. `PerceivedLoudnessSO`: `BeatSaber.Audio.Common` → `DataModels`) | PC moved some classes between assemblies; a wrong name leaves the object scriptless. Classes missing from the PS5 build stop the port. |
| Block-compressed textures re-encoded as linear RGBA32 with mips, `m_IsPreProcessed` off | PS5 stores BC textures GPU-tiled (1024² BC7 with 11 mips: 1,400,832 bytes on PS5 vs 1,398,128 linear). PC data would read out of bounds or render garbage. Uncompressed textures are linear on PS5. |
| Level covers rebuilt as standalone sprites + RGBA32 textures in the pack bundle | PC covers live in the PC shared-assets sprite atlas, which PS5 doesn't have. The sprite reuses the pack cover's quad (pixels-per-unit = width) and the bundle's preload table is extended. |
| Environments missing on PS5 remapped (`config.json`) | PC-only environments need scripts the PS5 build lacks plus Windows shaders; their bundles are never shipped. |
| `_tags` 2 → 3 when `unlock_all_dlc` | §2 |

An updated pack (every PS5 level plus more on PC, e.g. Shock Drop) replaces the PS5 bundle under the **PS5
file name**. The PC bundle has no PS5 store data or PS5 leaderboard IDs, so the levels the pack already had
lose their online leaderboards, and with `unlock_all_dlc` off a paid pack's owned levels would lock: the porter
then skips the update and warns. Grafting the PS5 store objects into the PC bundle would avoid both but needs
cross-bundle object surgery that hasn't been done.

## 6. Packaging and running

- `port.py` assembles original + changes and packs a debug FPKG with Dump Forge (`--kraken fast --full-verify`).
- On the console, KrakenMount serves it as a package image and builds its own AMPR asset index. **Run the
  game this way.** As an extracted folder, even the unmodified game crashed 2–4 s after launch: SIGSEGV in
  `psvr2.prx`, whose `UnityPluginLoad` fails `Load_Display`/`Load_Input` and nulls its state before
  `PSVR2Plugin.ApplySettings` writes through it. The cause wasn't proven (most likely file access through
  AMPR emulation in folder mode); the package route avoids it.
- A `backports/<titleID>` folder is overlaid on the game. Empty folders in it **hide** the game's own (one
  hid `Media/UnitySubsystems`). Only keep real backports there.

## 7. Verification

`validate.py` checks, without the console:

- every shipped bundle: stock layout flags, PS5 header (version, platform 44, type trees)
- every reference resolves (local, or to an existing PS5 object)
- every script exists in the PS5 metadata
- no pre-processed (GPU-tiled) textures
- converted textures are linear RGBA32, with sizes matching their mip chains
- every environment a level names (all three fields) exists on PS5
- song beatmaps are no newer than `max_beatmap_version`
- every pack in the final catalog: no DLC-only pack left locked when `unlock_all_dlc` is on, no duplicate pack or
  level IDs
- unchanged PS5 bundles differ only in the pack definition's `_tags`
- catalog: original entries untouched, new entries well-formed, `m_Crc` 0 and real sizes; every asset served
  by a shipped bundle is in that bundle's container, and its dependency list covers the bundle's external
  references; every AssetReference GUID resolvable
- song files: container asset name matches the level ID

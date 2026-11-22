#!/usr/bin/env python3
"""Add BeatSaver songs to the PS5 game as a new "Custom Songs" pack, delivered as a Kraken Mount overlay.

    python custom_songs.py --pkg "Beat Saber (v01.039.000) Modded.pkg" --songs songs --out work/custom_overlay

--pkg    the package that is INSTALLED on the console (the overlay's catalog is built from its catalog, so
         it keeps every pack that package has). Extracted once into work/installed/; reused while unchanged.
--base   instead of --pkg: an already extracted game folder (repeatable: later folders win).
--songs  a folder of BeatSaver downloads: .zip files or unzipped map folders, as many as you like.
--with   a porter overlay (port.py's work/overlay) whose packs should ride along: every file in it that
         differs from the installed package is copied into the output and its catalog becomes the base, so
         ported packs that were never baked into the installed PKG arrive with the custom songs.
--out    an empty folder; it receives Media/StreamingAssets/... with only the new and changed files:
           Media/StreamingAssets/aa/catalog.json                        the installed catalog + the pack
           Media/StreamingAssets/aa/PS5/customsongs_pack_assets_all_*.bundle    the pack (all songs)
           Media/StreamingAssets/BeatmapLevelsData/beatsaver<key>       one file per song
         Upload the contents of --out to backports/PPSA15035/ in the console folder that holds the
         package (e.g. /mnt/ext1/etaHEN/games/backports/PPSA15035/).

Every song becomes a new level in one new pack, so nothing in the game is replaced except the catalog.
The pack is modelled on the Prodigy pack (layout, credits page, promo info) and renamed so its asset-bundle
CAB, GUIDs and IDs cannot collide with the real one. The build ends with an offline check of everything it
wrote (references, audio headers, chart versions, catalog), and stops if anything is off.

Audio (--audio): vorbis (default), FMOD FSB5 Vorbis like the stock songs, about 3 MB per minute; or pcm,
FSB5 PCM16 like the stock game's uncompressed clips, about 10 MB per minute. See docs/CUSTOM-SONGS.md.
"""
import argparse
import copy
import filecmp
import glob
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib

import attr
import UnityPy
from PIL import Image
from UnityPy.streams import EndianBinaryReader

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from catalog_io import Catalog  # noqa: E402
from unity_bundles import (PS5_PLATFORM, iter_pptrs, new_path_id, rgba32_size, save_ps5, seeded_rng,  # noqa: E402
                           serialized_files, set_texture_rgba32, walk_pptrs)

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_ROOT = '{UnityEngine.AddressableAssets.Addressables.RuntimePath}'
SA = 'Media/StreamingAssets'
PACK_ID = 'CustomSongs'
PACK_NAME = 'Custom Songs'
PACK_PKG = 'com.beatgames.beatsaber.packs.customsongs'
PACK_FILE = 'customsongs_pack_assets_all_%s.bundle' % hashlib.md5(PACK_ID.encode()).hexdigest()
TEMPLATE_PACK = 'prodigy_pack_assets_all_'
TEMPLATE_SONG = 'houdini'
FREQ = 44100
CHANNELS = 2
DIFFS = {'Easy': 0, 'Normal': 1, 'Hard': 2, 'Expert': 3, 'ExpertPlus': 4}
# characteristics the PS5 build has (BeatmapCharacteristicSO._serializedName in sharedassets); rotation ones
# need an all-directions environment
CHARACTERISTICS = ('Standard', 'OneSaber', 'NoArrows', '360Degree', '90Degree', 'Legacy')
ROTATION = ('360Degree', '90Degree')
CREDITS_GAP = 8.0
CREDITS_LINE = 6.2
CREDITS_CHARS_PER_LINE = 38


class Fail(SystemExit):
    pass


def log(*a):
    print(*a, flush=True)


class Layers:
    def __init__(self, dirs):
        self.dirs = [os.path.abspath(d) for d in dirs]

    def path(self, rel):
        for d in reversed(self.dirs):
            p = os.path.join(d, rel)
            if os.path.exists(p):
                return p
        return None

    def glob(self, rel_pattern):
        found = {}
        for d in self.dirs:
            for p in glob.glob(os.path.join(d, rel_pattern)):
                found[os.path.relpath(p, d)] = p
        return found


def load_config():
    with open(os.path.join(HERE, 'config.json')) as f:
        return json.load(f)


def extract_installed(pkg, work, cfg):
    """Extract the installed package once into work/installed/<name>; reused while the package is unchanged."""
    pkg = os.path.realpath(pkg)
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', os.path.basename(pkg))[:48]
    dest = os.path.join(work, 'installed', '%s_%s' % (name, hashlib.md5(pkg.encode()).hexdigest()[:8]))
    stamp = dest + '.source'
    ident = '%s %d %d' % (pkg, os.path.getsize(pkg), int(os.path.getmtime(pkg)))
    if os.path.isdir(dest) and os.path.exists(stamp) and open(stamp).read() == ident:
        return dest
    if os.path.exists(dest):
        shutil.rmtree(dest)
    forge = cfg['dump_forge'] if os.path.isabs(cfg['dump_forge']) else os.path.join(HERE, cfg['dump_forge'])
    if not os.path.exists(forge):
        raise Fail('PS5 Dump Forge not found at %s (set "dump_forge" in config.json)' % forge)
    log('extracting %s (one time) ...' % os.path.basename(pkg))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    subprocess.run([forge, 'convert', pkg, '--to', 'folder', '-o', dest], check=True, stdout=subprocess.DEVNULL)
    with open(stamp, 'w') as f:
        f.write(ident)
    return dest


# ---------------------------------------------------------------- BeatSaver input

def version_tuple(s):
    parts = [int(x) for x in re.findall(r'\d+', s or '')[:3]]
    return tuple(parts + [0] * (3 - len(parts)))


def strip_bom(raw):
    return raw[3:] if raw[:3] == b'\xef\xbb\xbf' else raw


def read_files(path):
    """{lower-case name: (name, bytes)} of the folder holding Info.dat in a BeatSaver zip or an unzipped map folder."""
    if os.path.isdir(path):
        files = {}
        for n in os.listdir(path):
            p = os.path.join(path, n)
            if os.path.isfile(p) and not os.path.islink(p) and not n.startswith('._'):
                with open(p, 'rb') as f:
                    files[n.lower()] = (n, f.read())
        return files
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if not n.endswith('/') and not n.startswith('__MACOSX/')
                 and not n.split('/')[-1].startswith('._')]
        for n in names:
            if '..' in n.split('/') or n.startswith('/') or '\\' in n:
                raise Fail('unexpected path %r in the zip' % n)
        roots = sorted({n.rsplit('/', 1)[0] if '/' in n else '' for n in names if n.split('/')[-1].lower() == 'info.dat'})
        if len(roots) != 1:
            raise Fail('expected one Info.dat in the zip, found %d' % len(roots))
        root = roots[0] + '/' if roots[0] else ''
        files = {}
        for n in names:
            if n.startswith(root) and '/' not in n[len(root):]:
                base = n[len(root):]
                if base.lower() in files:
                    raise Fail('the zip holds %r twice (letter case differs)' % base)
                files[base.lower()] = (base, z.read(n))
        return files


def parse_color(c):
    """BeatSaver colour: {r,g,b,a} (Info 2.1), [r,g,b(,a)] or 'RRGGBB[AA]' hex (Info 4)."""
    if c is None:
        return None
    if isinstance(c, dict):
        return {k: float(c.get(k, 1.0 if k == 'a' else 0.0)) for k in 'rgba'}
    if isinstance(c, (list, tuple)) and len(c) in (3, 4):
        v = [float(x) for x in c]
        return dict(zip('rgba', v + [1.0] * (4 - len(v))))
    h = c.lstrip('#') if isinstance(c, str) else ''
    if not re.fullmatch(r'[0-9a-fA-F]{6}([0-9a-fA-F]{2})?', h):
        raise ValueError('bad colour %r' % (c,))
    v = [int(h[i:i + 2], 16) / 255.0 for i in range(0, len(h), 2)]
    return dict(zip('rgba', v + [1.0] * (4 - len(v))))


def ps5_color_scheme(cs, name):
    """BeatSaver colour scheme -> BeatmapLevelSO._colorSchemes item (field set taken from the stock levels)."""
    get = lambda *keys: next((parse_color(cs[k]) for k in keys if cs.get(k) is not None), None)
    sa, sb = get('saberAColor'), get('saberBColor')
    e0, e1 = get('environmentColor0'), get('environmentColor1')
    ob = get('obstaclesColor')
    white = {'r': 1.0, 'g': 1.0, 'b': 1.0, 'a': 1.0}
    notes = bool(cs.get('overrideNotes', sa is not None and sb is not None))
    lights = bool(cs.get('overrideLights', e0 is not None and e1 is not None))
    return {'_colorSchemeId': name, '_overrideNotes': int(notes and sa is not None and sb is not None),
            '_saberAColor': sa or white, '_saberBColor': sb or white, '_obstaclesColor': ob or white,
            '_overrideLights': int(lights and e0 is not None and e1 is not None),
            '_environmentColor0': e0 or white, '_environmentColor1': e1 or white,
            '_environmentColor0Boost': get('environmentColor0Boost') or e0 or white,
            '_environmentColor1Boost': get('environmentColor1Boost') or e1 or white}


def chart_version(chart):
    """The version the game's BeatmapSaveDataHelpers.GetVersion sees: "_version", else "version". A chart with
    neither goes to the game's v2 loader (it falls back to a version below 3.0.0), so report it as 2.0.0 when
    it has v2 content."""
    v = chart.get('_version') or chart.get('version')
    if v:
        return str(v)
    return '2.0.0' if '_notes' in chart else None


def chart_counts(c):
    if '_notes' in c:
        notes = [n for n in c['_notes'] if n.get('_type') in (0, 1)]
        bombs = [n for n in c['_notes'] if n.get('_type') == 3]
        return {'notes': len(notes), 'bombs': len(bombs), 'obstacles': len(c.get('_obstacles', [])), 'cuttable': len(notes)}
    if 'colorNotes' in c and isinstance(c.get('colorNotesData'), list):   # v4: index lists + data arrays
        notes, chains = c.get('colorNotes', []), c.get('chains', [])
        chain_data = c.get('chainsData', [])
        extra = sum(max(0, chain_data[ch.get('ci', 0)].get('c', 1) - 1) for ch in chains if ch.get('ci', 0) < len(chain_data))
        return {'notes': len(notes), 'bombs': len(c.get('bombNotes', [])), 'obstacles': len(c.get('obstacles', [])),
                'cuttable': len(notes) + extra}
    notes, chains = c.get('colorNotes', []), c.get('burstSliders', [])
    return {'notes': len(notes), 'bombs': len(c.get('bombNotes', [])), 'obstacles': len(c.get('obstacles', [])),
            'cuttable': len(notes) + sum(max(0, ch.get('sc', 1) - 1) for ch in chains)}


def needs_mods(cd):
    cd = cd or {}
    return cd.get('_requirements') or cd.get('requirements') or []


def map_key(path, info_raw):
    """The BeatSaver key from a download's name, e.g. '2f284 (Night Dancer - seasonsmouse & Tuc).zip' -> 2f284;
    any other name gets a key from the Info.dat contents."""
    name = re.sub(r'\.zip$', '', os.path.basename(path.rstrip('/')), flags=re.I)
    m = re.match(r'^([0-9a-f]{1,8}) \(.* - .*\)$', name)
    return m.group(1) if m else 'x' + hashlib.sha1(info_raw).hexdigest()[:7]


def read_song(path, max_chart):
    """Read one map. Raises Fail (naming the map) when the song as a whole can't be used; a difficulty that
    can't be used is skipped with a note in song['skipped']."""
    try:
        return _read_song(path, max_chart)
    except Fail as e:
        raise Fail('%s: %s' % (os.path.basename(path.rstrip('/')), e))
    except (KeyError, ValueError, TypeError, AttributeError, IndexError, OSError, zipfile.BadZipFile) as e:
        raise Fail('%s: malformed map (%s: %s)' % (os.path.basename(path.rstrip('/')), type(e).__name__, e))


def _read_song(path, max_chart):
    files = read_files(path)
    if 'info.dat' not in files:
        raise Fail('no Info.dat')
    info_raw = strip_bom(files['info.dat'][1])
    info = json.loads(info_raw.decode('utf-8'))
    key = map_key(path, info_raw)

    def get(name, what):
        f = files.get((name or '').lower())
        if f is None:
            raise Fail('%s %r named in Info.dat is missing' % (what, name))
        return strip_bom(f[1])

    song = {'key': key, 'path': path, 'level_id': 'BeatSaver' + key, 'diffs': [], 'skipped': []}
    if '_version' in info:
        song.update(
            name=info['_songName'], sub=info.get('_songSubName', ''), author=info['_songAuthorName'],
            mapper=info.get('_levelAuthorName', ''), bpm=float(info['_beatsPerMinute']),
            offset=float(info.get('_songTimeOffset', 0)), shuffle=float(info.get('_shuffle', 0)),
            shuffle_period=float(info.get('_shufflePeriod', 0.5)),
            preview_start=float(info.get('_previewStartTime', 0)), preview_len=float(info.get('_previewDuration', 10) or 10),
            audio=get(info['_songFilename'], 'song'), cover=get(info['_coverImageFilename'], 'cover'),
            audio_data=None, lufs=None)
        env_default = info.get('_environmentName') or 'DefaultEnvironment'
        env_all = info.get('_allDirectionsEnvironmentName') or 'GlassDesertEnvironment'
        env_list = info.get('_environmentNames') or []
        schemes = [(cs.get('colorScheme') or {}, cs.get('useOverride', True)) for cs in info.get('_colorSchemes') or []]
        sets = [(s['_beatmapCharacteristicName'], d) for s in info['_difficultyBeatmapSets'] for d in s['_difficultyBeatmaps']]
    elif 'version' in info:
        if version_tuple(info['version']) >= (5, 0, 0) or version_tuple(info['version']) < (4, 0, 0):
            raise Fail('Info.dat version %s is not one this tool knows (2.x or 4.x)' % info['version'])
        s, a = info['song'], info['audio']
        song.update(
            name=s['title'], sub=s.get('subTitle', ''), author=s.get('author', ''), mapper='', bpm=float(a['bpm']),
            offset=0.0, shuffle=0.0, shuffle_period=0.5,
            preview_start=float(a.get('previewStartTime', 0)), preview_len=float(a.get('previewDuration', 10) or 10),
            audio=get(a['songFilename'], 'song'), cover=get(info['coverImageFilename'], 'cover'),
            audio_data=get(a['audioDataFilename'], 'audio data') if a.get('audioDataFilename') else None,
            lufs=float(a['lufs']) if a.get('lufs') else None)
        env_default, env_all = 'DefaultEnvironment', 'GlassDesertEnvironment'
        env_list = info.get('environmentNames') or []
        schemes = [(cs, cs.get('useOverride', True)) for cs in info.get('colorSchemes') or []]
        sets = [(d['characteristic'], d) for d in info['difficultyBeatmaps']]
        mappers = []
        for d in info['difficultyBeatmaps']:
            for n in (d.get('beatmapAuthors') or {}).get('mappers') or []:
                n = str(n)
                if n not in mappers:
                    mappers.append(n)
        song['mapper'] = ', '.join(mappers)
    else:
        raise Fail('Info.dat has no "_version" or "version"')
    for k in ('name', 'sub', 'author', 'mapper'):
        song[k] = '' if song[k] is None else str(song[k]).strip()
    if not song['name']:
        raise Fail('Info.dat has no song name')
    try:
        Image.open(io.BytesIO(song['cover'])).load()
    except Exception as e:
        raise Fail('cover image unreadable (%s)' % e)
    if song['audio_data'] is not None:
        song['audio_data'] = parse_audio_data(song['audio_data'], song['skipped'])

    song['schemes'] = []
    scheme_map = {}
    for i, (cs, use) in enumerate(schemes):
        if not use:
            continue
        try:
            scheme = ps5_color_scheme(cs, cs.get('colorSchemeId') or cs.get('colorSchemeName') or '%s_%d' % (song['level_id'], i))
        except (ValueError, TypeError) as e:
            song['skipped'].append('colour scheme %d: %s' % (i, e))
            continue
        scheme_map[i] = len(song['schemes'])
        song['schemes'].append(scheme)

    seen = set()
    for ch, d in sets:
        dname = d.get('_difficulty') or d.get('difficulty')
        label = '%s %s' % (ch, dname)
        req = needs_mods(d.get('_customData') or d.get('customData'))
        if req:
            song['skipped'].append('%s: needs mods %s' % (label, ', '.join(map(str, req))))
            continue
        if ch not in CHARACTERISTICS:
            song['skipped'].append('%s: characteristic %s is not in the PS5 game' % (label, ch))
            continue
        if dname not in DIFFS:
            song['skipped'].append('%s: unknown difficulty' % label)
            continue
        if (ch, dname) in seen:
            song['skipped'].append('%s: listed twice in Info.dat; the first one is used' % label)
            continue
        try:
            raw = get(d.get('_beatmapFilename') or d.get('beatmapDataFilename'), 'chart')
            chart = json.loads(raw.decode('utf-8'))
        except (Fail, ValueError) as e:
            song['skipped'].append('%s: %s' % (label, e))
            continue
        ver = chart_version(chart)
        if ver is None or version_tuple(ver) > max_chart or version_tuple(ver) < (2, 0, 0):
            song['skipped'].append('%s: chart version %s is not one the PS5 game reads (2.x to %s)'
                                   % (label, ver, '.'.join(map(str, max_chart))))
            continue
        light = None
        if version_tuple(ver) >= (4, 0, 0) and d.get('lightshowDataFilename'):
            try:
                light = get(d['lightshowDataFilename'], 'lightshow')
                lver = chart_version(json.loads(light.decode('utf-8')))
            except (Fail, ValueError) as e:
                song['skipped'].append('%s: %s; playing without lights' % (label, e))
                light, lver = None, None
            if light is not None and (lver is None or version_tuple(lver) > max_chart):
                song['skipped'].append('%s: lightshow version %s is not readable; playing without lights' % (label, lver))
                light = None
        idx = d.get('_environmentNameIdx', d.get('environmentNameIdx'))
        if isinstance(idx, int) and 0 <= idx < len(env_list):
            env = env_list[idx]
        else:
            env = env_all if ch in ROTATION else env_default
        cidx = d.get('_beatmapColorSchemeIdx', d.get('beatmapColorSchemeIdx', -1))
        seen.add((ch, dname))
        song['diffs'].append({
            'characteristic': ch, 'name': dname, 'value': DIFFS[dname], 'version': ver,
            'njs': float(d.get('_noteJumpMovementSpeed', d.get('noteJumpMovementSpeed', 0)) or 0),
            'offset': float(d.get('_noteJumpStartBeatOffset', d.get('noteJumpStartBeatOffset', 0)) or 0),
            'raw': raw, 'light': light, 'env': env, 'scheme': scheme_map.get(cidx, -1) if isinstance(cidx, int) else -1,
            'counts': chart_counts(chart)})
    if not song['diffs']:
        raise Fail('no difficulty the PS5 game can play (%s)' % '; '.join(song['skipped']))
    song['diffs'].sort(key=lambda d: (CHARACTERISTICS.index(d['characteristic']), d['value']))
    return song


# ---------------------------------------------------------------- audio

def decode_pcm(audio_bytes, start=None, length=None, fade=False):
    cmd = ['ffmpeg', '-v', 'error', '-nostdin']
    if start is not None:
        cmd += ['-ss', '%.3f' % max(0.0, start)]
    cmd += ['-i', 'pipe:0']
    if length is not None:
        cmd += ['-t', '%.3f' % length]
    if fade and length:
        cmd += ['-af', 'afade=t=in:d=0.5,afade=t=out:st=%.3f:d=1' % max(0.0, length - 1)]
    cmd += ['-f', 's16le', '-acodec', 'pcm_s16le', '-ac', str(CHANNELS), '-ar', str(FREQ), 'pipe:1']
    r = subprocess.run(cmd, input=audio_bytes, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        lines = [l for l in r.stderr.decode(errors='replace').splitlines() if l.strip()]
        raise Fail('ffmpeg could not decode the audio (%s)' % (lines[0] if lines else 'no output'))
    return r.stdout


def integrated_lufs(audio_bytes):
    r = subprocess.run(['ffmpeg', '-v', 'info', '-nostdin', '-i', 'pipe:0', '-af', 'ebur128', '-f', 'null', '-'],
                       input=audio_bytes, capture_output=True)
    m = re.findall(r'I:\s+(-?[\d.]+) LUFS', r.stderr.decode(errors='replace'))
    return float(m[-1]) if m else None


CLIP_FORMAT = {'vorbis': (1, 1), 'pcm': (0, 0)}   # AudioClip (m_CompressionFormat, m_LoadType)
FSB_FREQ = {8000: 1, 11000: 2, 11025: 3, 16000: 4, 22050: 5, 24000: 6, 32000: 7, 44100: 8, 48000: 9, 96000: 10}


def fsb5_pcm16(pcm):
    """One-sample FSB5 v1, mode 2 (PCM16), laid out like the stock game's own PCM clips."""
    frame = 2 * CHANNELS
    samples = len(pcm) // frame
    data = pcm[:samples * frame]
    data += b'\0' * ((-len(data)) % 32)
    v = (FSB_FREQ[FREQ] << 1) | ((1 if CHANNELS == 2 else 0) << 5) | (samples << 34)
    shdr = struct.pack('<Q', v) + b'\0' * 28
    head = b'FSB5' + struct.pack('<6I', 1, 1, len(shdr), 0, len(data), 2) + b'\0' * 8 + hashlib.md5(data).digest() + b'\0' * 8
    assert len(head) == 0x3c
    return head + shdr + data, samples


# FMOD does not store the Vorbis setup header: it keeps a table of the headers its own encoder makes and finds
# the right one by the header's CRC32, kept in the FSB5's VORBISDATA chunk. libvorbis at quality 1.0..0.9
# (libsndfile compression_level 0.0..0.1), 44.1 kHz stereo, makes the very header the stock songs name
# (CRC 0x6D39BF3E, e.g. BeatmapLevelsData/houdini), so the game's FMOD decodes it like its own songs.
VORBIS_LEVEL = 0.0
VORBIS_SETUP_CRC = 0x6D39BF3E
FSB_MODE_PCM16, FSB_MODE_VORBIS = 2, 15
FSB_CHUNK_VORBISDATA = 11


def ogg_packets(b):
    """Packets of an Ogg stream, in order."""
    out, cur, pos = [], bytearray(), 0
    while pos + 27 <= len(b):
        if b[pos:pos + 4] != b'OggS':
            raise ValueError('bad Ogg page')
        n = b[pos + 26]
        d = pos + 27 + n
        for seg in b[pos + 27:pos + 27 + n]:
            cur += b[d:d + seg]
            d += seg
            if seg < 255:
                out.append(bytes(cur))
                cur = bytearray()
        pos = d
    return out


def vorbis_modes(id_pkt, setup_pkt):
    """(blocksize 0, blocksize 1, blockflag of every mode). The mode table ends the setup header; it is read
    backwards from the framing bit, as ffmpeg's vorbis parser does."""
    bs0, bs1 = 1 << (id_pkt[28] & 0x0F), 1 << (id_pkt[28] >> 4)
    bit = lambda i: (setup_pkt[i >> 3] >> (i & 7)) & 1
    field = lambda start, n: sum(bit(start + k) << k for k in range(n))
    f = len(setup_pkt) * 8 - 1
    while f >= 0 and not bit(f):
        f -= 1
    best = None
    for m in range(1, 65):
        p = f - 41 * m                      # a mode: blockflag 1, windowtype 16, transformtype 16, mapping 8 bits
        if p - 6 < 0 or field(p + 33, 8) > 63 or field(p + 1, 16) or field(p + 17, 16):
            break
        if field(p - 6, 6) == m - 1:
            best = m
    if best is None:
        raise ValueError('Vorbis mode table not found')
    start = f - 41 * best
    return bs0, bs1, [bit(start + 41 * i) for i in range(best)]


def vorbis_positions(packets, bs0, bs1, flags):
    """Decoder output position after each audio packet (the first packet returns nothing)."""
    mbits = (len(flags) - 1).bit_length()          # ilog(modes - 1): 0 bits when there is one mode
    out, pos, prev = [], 0, None
    for pk in packets:
        if not pk:
            raise ValueError('empty Vorbis packet')
        cur = bs1 if flags[(pk[0] >> 1) & ((1 << mbits) - 1)] else bs0
        if prev is not None:
            pos += prev // 4 + cur // 4
        out.append(pos)
        prev = cur
    return out


_VORBIS_HEADERS = []


def vorbis_headers():
    """The identification, comment and setup headers of our encoder (the same for every song)."""
    if not _VORBIS_HEADERS:
        pkts = ogg_packets(encode_ogg(b'\0' * 4 * FREQ))
        _VORBIS_HEADERS.extend(pkts[:3])
    return _VORBIS_HEADERS


def encode_ogg(pcm):
    import numpy as np
    import soundfile
    x = np.frombuffer(pcm, '<i2').reshape(-1, CHANNELS)
    buf = io.BytesIO()
    with soundfile.SoundFile(buf, 'w', samplerate=FREQ, channels=CHANNELS, format='OGG', subtype='VORBIS',
                             compression_level=VORBIS_LEVEL) as w:
        for i in range(0, len(x), 8192):     # chunked: one big write overflows libsndfile's stack on long songs
            w.write(x[i:i + 8192])
    return buf.getvalue()


def fsb5_file(mode, samples, chunks, data):
    """One-sample FSB5 v1 with the stock layout: sample header and data each padded to 32 bytes."""
    v = (FSB_FREQ[FREQ] << 1) | ((1 if CHANNELS == 2 else 0) << 5) | (samples << 34) | (1 if chunks else 0)
    shdr = bytearray(struct.pack('<Q', v))
    for i, (kind, body) in enumerate(chunks):
        more = 1 if i + 1 < len(chunks) else 0
        shdr += struct.pack('<I', more | (len(body) << 1) | (kind << 25)) + body
    shdr += b'\0' * ((-(0x3c + len(shdr))) % 32)
    data = data + b'\0' * ((-len(data)) % 32)
    flag = b'\x01' + b'\0' * 7 if mode == FSB_MODE_VORBIS else b'\0' * 8
    head = b'FSB5' + struct.pack('<6I', 1, 1, len(shdr), 0, len(data), mode) + flag + hashlib.md5(data).digest() + b'\0' * 8
    return head + bytes(shdr) + data


def fsb5_vorbis(pcm):
    """FSB5 Vorbis (mode 15) like the stock songs: [u16 size][packet] stream ending in a zero size, and a
    VORBISDATA chunk with the setup CRC and a seek table (per whole second: the last packet that starts
    before it, as (sample position, byte offset))."""
    samples = len(pcm) // (2 * CHANNELS)
    pkts = ogg_packets(encode_ogg(pcm[:samples * 2 * CHANNELS]))
    idp, _, setup = pkts[:3]
    crc = zlib.crc32(setup) & 0xFFFFFFFF
    if crc != VORBIS_SETUP_CRC:
        raise Fail('the Vorbis encoder made setup header %08x, not FMOD\'s %08x (libsndfile/libvorbis version?);'
                   ' use --audio pcm' % (crc, VORBIS_SETUP_CRC))
    audio = pkts[3:]
    for pk in audio:
        if not 0 < len(pk) <= 255 * 255:       # FSB5 sizes are u16; the rebuilt Ogg pages of the check need <= 65025
            raise Fail('Vorbis packet of %d bytes' % len(pk))
    bs0, bs1, flags = vorbis_modes(idp, setup)
    pos = vorbis_positions(audio, bs0, bs1, flags)
    data, offs = bytearray(), []
    for pk in audio:
        offs.append(len(data))
        data += struct.pack('<H', len(pk)) + pk
    data += b'\0\0'
    seek = b''.join(struct.pack('<II', *e) for e in vorbis_seek_table(pos, offs, samples))
    body = struct.pack('<II', crc, len(seek)) + seek
    return fsb5_file(FSB_MODE_VORBIS, samples, [(FSB_CHUNK_VORBISDATA, body)], bytes(data)), samples


def vorbis_seek_table(pos, offs, samples):
    """Per whole second k: (position, byte offset) of the last packet whose position is before k seconds
    (reproduces the stock files' tables)."""
    out, j = [], 0
    for k in range(1, samples // FREQ + 1):
        while j + 1 < len(pos) and pos[j + 1] < k * FREQ:
            j += 1
        out.append((pos[j], offs[j]))
    return out


def vorbis_preflight():
    """Fail early (before any song is built) when the Vorbis encoder is missing or makes the wrong header."""
    try:
        import soundfile  # noqa: F401
    except ImportError:
        raise Fail('the soundfile package is needed for Vorbis audio (.venv/bin/pip install -r requirements.txt),'
                   ' or use --audio pcm')
    crc = zlib.crc32(vorbis_headers()[2]) & 0xFFFFFFFF
    if crc != VORBIS_SETUP_CRC:
        raise Fail('this libsndfile/libvorbis makes Vorbis setup header %08x, not FMOD\'s %08x; use --audio pcm'
                   % (crc, VORBIS_SETUP_CRC))


def fsb5_audio(pcm, kind):
    if kind == 'pcm':
        return fsb5_pcm16(pcm)
    return fsb5_vorbis(pcm)


def packet_offsets(packets):
    out, n = [], 0
    for pk in packets:
        out.append(n)
        n += 2 + len(pk)
    return out


def ogg_page(data, gran, seq, flags):
    lacing = [255] * (len(data) // 255) + [len(data) % 255]
    page = bytearray(b'OggS' + bytes([0, flags]) + struct.pack('<qIII', gran, 1, seq, 0) + bytes([len(lacing)])
                     + bytes(lacing) + data)
    struct.pack_into('<I', page, 22, ogg_crc(bytes(page)))
    return bytes(page)


_OGG_CRC = []


def ogg_crc(b):
    if not _OGG_CRC:
        for i in range(256):
            r = i << 24
            for _ in range(8):
                r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if r & 0x80000000 else (r << 1) & 0xFFFFFFFF
            _OGG_CRC.append(r)
    c = 0
    for x in b:
        c = ((c << 8) & 0xFFFFFFFF) ^ _OGG_CRC[((c >> 24) & 0xFF) ^ x]
    return c


def fsb5_vorbis_decode(fsb):
    """Decode an FSB5 Vorbis clip the way FMOD does (setup header looked up by CRC; here: ours, which must
    match), through a rebuilt Ogg stream and ffmpeg. Returns (frames decoded, ffmpeg error lines)."""
    h = parse_fsb5(fsb)
    idp, comment, setup = vorbis_headers()
    if h['vorbis_crc'] != zlib.crc32(setup) & 0xFFFFFFFF:
        return 0, ['setup CRC %08x is not the encoder\'s' % h['vorbis_crc']]
    bs0, bs1, flags = vorbis_modes(idp, setup)
    pos = vorbis_positions(h['packets'], bs0, bs1, flags)
    pages = [ogg_page(idp, 0, 0, 2), ogg_page(comment, 0, 1, 0), ogg_page(setup, 0, 2, 0)]
    for i, pk in enumerate(h['packets']):
        last = i + 1 == len(h['packets'])
        pages.append(ogg_page(pk, h['samples'] if last else pos[i], 3 + i, 4 if last else 0))
    r = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', 'pipe:0', '-f', 's16le', '-ac', str(CHANNELS), 'pipe:1'],
                       input=b''.join(pages), capture_output=True)
    return len(r.stdout) // (2 * CHANNELS), [l for l in r.stderr.decode(errors='replace').splitlines() if l.strip()]


def parse_fsb5(b):
    """Header fields of a one-sample FSB5; for Vorbis also the setup CRC, seek table and packets."""
    if b[:4] != b'FSB5':
        raise ValueError('not FSB5')
    ver, ns, shs, nts, ds, mode = struct.unpack_from('<6I', b, 4)
    v = struct.unpack_from('<Q', b, 0x3c)[0]
    h = {'version': ver, 'count': ns, 'mode': mode, 'freq_idx': (v >> 1) & 0xF, 'stereo': (v >> 5) & 1,
         'samples': v >> 34, 'data_size': ds, 'total': 0x3c + shs + nts + ds}
    p, more = 0x44, v & 1
    while more:
        c = struct.unpack_from('<I', b, p)[0]
        more, size, kind = c & 1, (c >> 1) & 0xFFFFFF, c >> 25
        if kind == FSB_CHUNK_VORBISDATA:
            crc, n = struct.unpack_from('<II', b, p + 4)
            h['vorbis_crc'] = crc
            h['seek'] = [struct.unpack_from('<II', b, p + 12 + 8 * i) for i in range(n // 8)]
        p += 4 + size
    if mode == FSB_MODE_VORBIS:
        data, q, pkts = b[0x3c + shs + nts:0x3c + shs + nts + ds], 0, []
        while q + 2 <= len(data):
            n = struct.unpack_from('<H', data, q)[0]
            if not n:
                break
            pkts.append(bytes(data[q + 2:q + 2 + n]))
            q += 2 + n
        h['packets'] = pkts
    return h


def parse_audio_data(raw, notes):
    """A v4 AudioData file, checked; None (one BPM region from Info.dat is used instead) when it is unusable."""
    try:
        j = json.loads(raw.decode('utf-8'))
        regions = j['bpmData']
        for r in regions:
            for k in ('si', 'ei', 'sb', 'eb'):
                float(r[k])
            if not r['ei'] > r['si']:
                raise ValueError('a BPM region ends before it starts')
        for r in j.get('lufsData') or []:
            float(r['si']), float(r['ei'])
        if not regions or float(j.get('songFrequency') or FREQ) <= 0:
            raise ValueError('no BPM regions')
        return j
    except (ValueError, KeyError, TypeError) as e:
        notes.append('audio data unusable (%s: %s); using the Info.dat BPM' % (type(e).__name__, e))
        return None


def audio_data_json(song, samples):
    """v4 AudioData: the map's own, sample positions rescaled to our 44.1 kHz audio and cut at its end (a cut
    region keeps its tempo: its end beat is moved with it), or one BPM region."""
    if song['audio_data']:
        j = copy.deepcopy(song['audio_data'])
        scale = FREQ / float(j.get('songFrequency') or FREQ)
        regions = []
        for r in j['bpmData']:
            si, ei = float(r['si']) * scale, float(r['ei']) * scale
            if si >= samples:
                continue
            sb, eb = float(r['sb']), float(r['eb'])
            if ei > samples:
                eb = sb + (eb - sb) * (samples - si) / (ei - si)
                ei = samples
            regions.append({'si': int(round(si)), 'ei': int(round(ei)), 'sb': sb, 'eb': eb})
        lufs = []
        for r in j.get('lufsData') or []:
            si, ei = float(r['si']) * scale, min(float(r['ei']) * scale, samples)
            if si < samples:
                lufs.append(dict(r, si=int(round(si)), ei=int(round(ei))))
        if regions:
            j.update(version=j.get('version') or '4.0.0', songChecksum='', songSampleCount=samples, songFrequency=FREQ,
                     bpmData=regions, lufsData=lufs)
            return j
        song['skipped'].append('audio data: every BPM region starts after the song ends; using the Info.dat BPM')
    beats = samples / float(FREQ) * song['bpm'] / 60.0
    return {'version': '4.0.0', 'songChecksum': '', 'songSampleCount': samples, 'songFrequency': FREQ,
            'bpmData': [{'si': 0, 'ei': samples, 'sb': 0.0, 'eb': beats}], 'lufsData': []}


# ---------------------------------------------------------------- bundle helpers

def rename_cab(env, new):
    bf = env.file
    old = next(k for k, v in bf.files.items() if hasattr(v, 'objects'))
    bf.files = {(new + k[len(old):] if k.startswith(old) else k): v for k, v in bf.files.items()}
    for k, v in bf.files.items():
        if hasattr(v, 'name'):
            try:
                v.name = k
            except Exception:
                pass
    return old


def set_resource(env, cab, suffix, data):
    old = env.file.files.get(cab + suffix)
    r = EndianBinaryReader(data)
    r.flags = getattr(old, 'flags', 0) if old is not None else 0
    env.file.files[cab + suffix] = r


def clone_object(sf, tmpl_pid, new_pid, tree):
    o = attr.evolve(sf.objects[tmpl_pid], path_id=new_pid, data=None)
    o.save_typetree(tree)
    sf.objects[new_pid] = o
    return o


def reachable(trees, roots):
    seen, stack = set(), list(roots)
    while stack:
        pid = stack.pop()
        if pid in seen or pid not in trees:
            continue
        seen.add(pid)
        for p in iter_pptrs(trees[pid]):
            if p['m_FileID'] == 0 and p['m_PathID']:
                stack.append(p['m_PathID'])
    return seen


def preload_for(trees, root):
    deps = reachable(trees, [root])
    ext = set()
    for pid in deps:
        for p in iter_pptrs(trees[pid]):
            if p['m_FileID'] > 0:
                ext.add((p['m_FileID'], p['m_PathID']))
    return [{'m_FileID': 0, 'm_PathID': pid} for pid in sorted(deps)] + [{'m_FileID': f, 'm_PathID': p} for f, p in sorted(ext)]


def gz_text(raw):
    return gzip.compress(raw, mtime=0).decode('utf-8', 'surrogateescape')


# ---------------------------------------------------------------- song bundle

def build_song_bundle(layers, song, out_dir, audio):
    tmpl = layers.path('%s/BeatmapLevelsData/%s' % (SA, TEMPLATE_SONG))
    if not tmpl:
        raise Fail('template song bundle %s not found in the game' % TEMPLATE_SONG)
    env = UnityPy.load(tmpl)
    (_, sf), = serialized_files(env)
    trees = {pid: o.read_typetree() for pid, o in sf.objects.items()}
    kinds = {pid: o.type.name for pid, o in sf.objects.items()}
    ab_pid = next(p for p, k in kinds.items() if k == 'AssetBundle')
    mb_pid = next(p for p, k in kinds.items() if k == 'MonoBehaviour' and '_difficultyBeatmapSets' in trees[p])
    clip_pid = next(p for p, k in kinds.items() if k == 'AudioClip')
    mb = trees[mb_pid]
    audio_ta = mb['_audioDataAsset']['m_PathID']
    text_tmpl = mb['_difficultyBeatmapSets'][0]['_difficultyBeatmaps'][0]['_beatmapAsset']['m_PathID']
    lid, lid_l = song['level_id'], song['level_id'].lower()
    rng = seeded_rng('custom-song', lid)
    used = set(sf.objects)

    pcm = decode_pcm(song['audio'])
    fsb, samples = fsb5_audio(pcm, audio)
    del pcm
    song['duration'] = samples / float(FREQ)
    if song['lufs'] is None:
        song['lufs'] = integrated_lufs(song['audio'])

    cab = 'CAB-' + hashlib.md5(('custom-song|' + lid).encode()).hexdigest()
    old_cab = rename_cab(env, cab)
    for k in [k for k in env.file.files if k.startswith(cab) and k != cab]:
        del env.file.files[k]
    set_resource(env, cab, '.resource', fsb)

    t = trees[clip_pid]
    # Vorbis: compressed in memory, as the stock songs. PCM: decompress on load, as the stock PCM clips
    # (console-tested that way).
    t.update(m_Name=song['name'][:60], m_LoadType=CLIP_FORMAT[audio][1], m_Channels=CHANNELS, m_Frequency=FREQ,
             m_BitsPerSample=16, m_Length=song['duration'], m_CompressionFormat=CLIP_FORMAT[audio][0],
             m_PreloadAudioData=False, m_LoadInBackground=True)
    t['m_Resource'] = {'m_Source': 'archive:/%s/%s.resource' % (cab, cab), 'm_Offset': 0, 'm_Size': len(fsb)}
    trees[audio_ta]['m_Name'] = '%s.audio.gz' % lid
    trees[audio_ta]['m_Script'] = gz_text(json.dumps(audio_data_json(song, samples), indent=4).encode())

    keep = {ab_pid, mb_pid, clip_pid, audio_ta} | {p for p, k in kinds.items() if k == 'MonoScript'}
    texts = {}

    def text_asset(name, raw):
        h = hashlib.sha1(raw).hexdigest()
        if h in texts:
            return texts[h]
        pid = new_path_id(used, rng)
        tt = copy.deepcopy(trees[text_tmpl])
        tt['m_Name'] = name
        tt['m_Script'] = gz_text(raw)
        clone_object(sf, text_tmpl, pid, tt)
        trees[pid], kinds[pid] = tt, 'TextAsset'
        keep.add(pid)
        texts[h] = pid
        return pid

    sets = []
    for d in song['diffs']:
        b = text_asset('%s%s.beatmap.gz' % (d['characteristic'], d['name']), d['raw'])
        lpid = text_asset('%s%s.lightshow.gz' % (d['characteristic'], d['name']), d['light']) if d['light'] else 0
        if not sets or sets[-1]['_beatmapCharacteristicSerializedName'] != d['characteristic']:
            sets.append({'_beatmapCharacteristicSerializedName': d['characteristic'], '_difficultyBeatmaps': []})
        sets[-1]['_difficultyBeatmaps'].append({'_difficulty': d['value'], '_beatmapAsset': {'m_FileID': 0, 'm_PathID': b},
                                                '_lightshowAsset': {'m_FileID': 0, 'm_PathID': lpid}})
    mb['m_Name'] = '%sBeatmapLevelData' % lid
    mb['_difficultyBeatmapSets'] = sets

    for pid in list(sf.objects):
        if pid not in keep:
            del sf.objects[pid]
            trees.pop(pid, None)
    ab = trees[ab_pid]
    ab['m_Name'] = ab['m_AssetBundleName'] = lid_l
    ab['m_PreloadTable'] = preload_for({p: trees[p] for p in trees if p != ab_pid}, mb_pid)
    ab['m_Container'] = [['packages/%s/so/%s/%sbeatmapleveldata.asset' % (PACK_PKG, lid_l, lid_l),
                          {'preloadIndex': 0, 'preloadSize': len(ab['m_PreloadTable']), 'asset': {'m_FileID': 0, 'm_PathID': mb_pid}}]]
    for pid in trees:
        sf.objects[pid].save_typetree(trees[pid])
    sf.mark_changed()
    os.makedirs(os.path.join(out_dir, SA, 'BeatmapLevelsData'), exist_ok=True)
    dst = os.path.join(out_dir, SA, 'BeatmapLevelsData', lid_l)
    save_ps5(env, dst)
    return {'level_id': lid, 'source': os.path.basename(song['path'].rstrip(os.sep)), 'file': os.path.relpath(dst, out_dir),
            'size': os.path.getsize(dst), 'cab': cab, 'template_cab': old_cab, 'seconds': round(song['duration'], 2),
            'lufs': song['lufs'], 'difficulties': ['%s %s (chart %s%s)' % (d['characteristic'], d['name'], d['version'],
                                                                        ', lightshow' if d['light'] else '') for d in song['diffs']],
            'skipped': song['skipped']}


# ---------------------------------------------------------------- pack bundle

def cover_image(song, size):
    img = Image.open(io.BytesIO(song['cover'])).convert('RGBA')
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    return img.resize((size, size), Image.LANCZOS)


def pack_cover(songs, size):
    if len(songs) < 4:
        return cover_image(songs[0], size)
    img = Image.new('RGBA', (size, size))
    half = size // 2
    for i, s in enumerate(songs[:4]):
        img.paste(cover_image(s, half), ((i % 2) * half, (i // 2) * half))
    return img


def banner_image(songs, w, h):
    img = Image.new('RGBA', (w, h), (12, 12, 20, 255))
    img.paste(pack_cover(songs, w), (0, (h - w) // 2))
    return img


def characteristic_pptrs(layers, sf, std_pptr):
    """{serializedName: PPtr} for the characteristics in the external file the template's Standard points into."""
    ext = [e.path.split('/')[-1].lower() for e in sf.externals]
    cab = ext[std_pptr['m_FileID'] - 1]
    for p in layers.glob(SA + '/aa/PS5/sharedassets_*.bundle').values():
        env = UnityPy.load(p)
        for sub, f in serialized_files(env):
            if sub.lower() != cab:
                continue
            out = {}
            for pid, o in f.objects.items():
                if o.type.name == 'MonoBehaviour':
                    try:
                        t = o.read_typetree()
                    except Exception:
                        continue
                    if '_serializedName' in t and '_characteristicNameLocalizationKey' in t:
                        out[t['_serializedName']] = {'m_FileID': std_pptr['m_FileID'], 'm_PathID': pid}
            if out.get('Standard') != std_pptr:
                raise Fail('characteristic lookup: Standard is not where the template points')
            return out
    raise Fail('characteristic lookup: %s not found among the sharedassets bundles' % cab)


def credits_lines(text):
    return sum(max(1, math.ceil(len(line) / float(CREDITS_CHARS_PER_LINE))) for line in text.split('\n'))


def build_credits(sf, trees, kinds, used, rng, ab, songs):
    credits_root = next(info['asset']['m_PathID'] for name, info in ab['m_Container'] if name.endswith('.prefab'))
    go = trees[credits_root]
    go['m_Name'] = '%sCredits' % PACK_ID
    root_rt = next(c['component']['m_PathID'] for c in go['m_Component'] if kinds[c['component']['m_PathID']] == 'RectTransform')
    go_of = lambda rt: trees[rt]['m_GameObject']['m_PathID']
    comps = lambda g: [c['component']['m_PathID'] for c in trees[g]['m_Component']]
    text_mb = lambda rt: next(c for c in comps(go_of(rt)) if 'm_text' in trees.get(c, {}))
    children = [c['m_PathID'] for c in trees[root_rt]['m_Children']]
    header = next(rt for rt in children if trees[go_of(rt)]['m_Name'].startswith('CreditsHeaderItem'))
    titles = [rt for rt in children if trees[go_of(rt)]['m_Name'] == 'CreditsTitleItem']
    texts = [rt for rt in children if trees[go_of(rt)]['m_Name'] == 'CreditsTextItem']
    y = lambda rt: trees[rt]['m_AnchoredPosition']['y']
    x = lambda rt: trees[rt]['m_AnchorMin']['x']
    top = max(y(t) for t in titles)
    cols = sorted({x(t) for t in titles})
    tmpl = {cx: (next(t for t in titles if x(t) == cx and y(t) == top),
                 max((t for t in texts if x(t) == cx and y(t) < top), key=y)) for cx in cols}
    title_h = trees[tmpl[cols[0]][0]]['m_SizeDelta']['y']
    trees[text_mb(header)]['m_text'] = '%s Music Pack' % PACK_NAME
    trees[go_of(header)]['m_Name'] = 'CreditsHeaderItem (%s Music Pack)' % PACK_NAME

    def clone_item(rt):
        g = go_of(rt)
        old = [g] + comps(g)
        remap = {o: new_path_id(used, rng) for o in old}
        for o in old:
            t = copy.deepcopy(trees[o])
            walk_pptrs(t, lambda p: {'m_FileID': 0, 'm_PathID': remap[p['m_PathID']]} if p['m_FileID'] == 0 and p['m_PathID'] in remap else None)
            clone_object(sf, o, remap[o], t)
            trees[remap[o]], kinds[remap[o]] = t, kinds[o]
        return remap[rt]

    new_children = [header]
    row_top, bottom = top, top
    for i, s in enumerate(songs):
        cx = cols[i % len(cols)]
        if i and i % len(cols) == 0:
            row_top = bottom - CREDITS_GAP
        t_rt, x_rt = (clone_item(r) for r in tmpl[cx])
        body = 'Performed by %s\nMapped by %s' % (s['author'] or 'unknown', s['mapper'] or 'unknown')
        if not s['key'].startswith('x'):
            body += '\nBeatSaver %s' % s['key']
        trees[text_mb(t_rt)]['m_text'] = s['name'] + (' ' + s['sub'] if s['sub'] else '')
        trees[text_mb(x_rt)]['m_text'] = body
        h = title_h + CREDITS_LINE * (credits_lines(body) - 1)
        trees[t_rt]['m_AnchoredPosition'] = {'x': 0.0, 'y': row_top}
        trees[x_rt]['m_AnchoredPosition'] = {'x': 0.0, 'y': row_top - title_h}
        trees[x_rt]['m_SizeDelta'] = dict(trees[x_rt]['m_SizeDelta'], y=h)
        bottom = min(bottom if i % len(cols) else row_top, row_top - title_h - h)
        new_children += [t_rt, x_rt]
    for rt in new_children:
        trees[rt]['m_Father'] = {'m_FileID': 0, 'm_PathID': root_rt}
    trees[root_rt]['m_Children'] = [{'m_FileID': 0, 'm_PathID': c} for c in new_children]
    trees[root_rt]['m_SizeDelta'] = dict(trees[root_rt]['m_SizeDelta'], y=-bottom + 2 * CREDITS_GAP)


def build_pack_bundle(layers, songs, envs, cfg, out_dir, audio):
    found = [p for rel, p in layers.glob(SA + '/aa/PS5/*.bundle').items() if os.path.basename(rel).startswith(TEMPLATE_PACK)]
    if len(found) != 1:
        raise Fail('template pack %s* not found exactly once in the game (%d); it comes with the ported DLC packs'
                   % (TEMPLATE_PACK, len(found)))
    env = UnityPy.load(found[0])
    (_, sf), = serialized_files(env)
    trees = {pid: o.read_typetree() for pid, o in sf.objects.items()}
    kinds = {pid: o.type.name for pid, o in sf.objects.items()}
    rng = seeded_rng('custom-pack', *[s['level_id'] for s in songs])
    used = set(sf.objects)
    ab_pid = next(p for p, k in kinds.items() if k == 'AssetBundle')
    ab = trees[ab_pid]
    packdef = next(t for t in trees.values() if '_levelProductPacks' in t and '_tags' in t)
    pack = trees[packdef['_beatmapLevelPack']['m_PathID']]
    coll = trees[pack['_beatmapLevelCollection']['m_PathID']]
    tmpl_level = coll['_beatmapLevels'][0]['m_PathID']
    lt = trees[tmpl_level]
    tmpl_cover_spr = lt['_coverImage']['m_PathID']
    tmpl_cover_tex = trees[tmpl_cover_spr]['m_RD']['texture']['m_PathID']
    tmpl_clip = lt['_previewAudioClip']['m_PathID']
    chars = characteristic_pptrs(layers, sf, lt['_previewDifficultyBeatmapSets'][0]['_beatmapCharacteristic'])
    missing = sorted({d['characteristic'] for s in songs for d in s['diffs']} - set(chars))
    if missing:
        raise Fail('characteristics %s are not in this PS5 build' % missing)
    cab = 'CAB-' + hashlib.md5(('custom-pack|' + PACK_ID).encode()).hexdigest()
    old_cab = rename_cab(env, cab)

    guids = {k: hashlib.md5(('custom-pack-%s|%s' % (k, PACK_ID)).encode()).hexdigest() for k in ('def', 'promo', 'credits')}
    packdef['m_Name'] = '%s_PackDefinition' % PACK_ID
    packdef['_tags'] = 3                      # built-in + DLC bits: Music Packs tab, always owned
    packdef['_order'] = 100000                # after every official pack
    packdef['_packPromoInfoReference']['m_AssetGUID'] = guids['promo']
    packdef['_credits']['creditsPrefab']['m_AssetGUID'] = guids['credits']
    packdef['_credits']['order'] = 100000
    for k, p in packdef['_leaderboardIds'].items():
        trees[p['m_PathID']]['m_Name'] = '%s_%s' % (PACK_ID, k.strip('_'))
        trees[p['m_PathID']]['_leaderboardIds'] = []
    for k, p in packdef['_levelProductPacks'].items():
        t = trees[p['m_PathID']]
        t['m_Name'] = '%s_%s' % (PACK_ID, k.strip('_'))
        t['_levelPackProductData']['_levelPackId'] = PACK_ID
        t['_levelPackProductData']['_levelProductsData'] = []
    pack.update(m_Name='%sBeatmapLevelPack' % PACK_ID, _packID=PACK_ID, _packName=PACK_NAME, _shortPackName=PACK_NAME)
    coll['m_Name'] = '%sBeatmapLevelCollection' % PACK_ID
    for spr_key, suffix in (('_coverImage', 'Cover'), ('_smallCoverImage', 'CoverSmall')):
        spr = trees[pack[spr_key]['m_PathID']]
        tex = trees[spr['m_RD']['texture']['m_PathID']]
        set_texture_rgba32(tex, pack_cover(songs, tex['m_Width']))
        tex['m_Name'] = spr['m_Name'] = PACK_ID + suffix

    loud = trees[packdef['_perceivedLoudnessPerLevel']['m_PathID']]
    loud['m_Name'] = '%s_PerceivedLoudness' % PACK_ID
    loud['_perceivedLoudnessPerLevel'] = []
    new_levels, res, notes = [], b'', []
    for s in songs:
        env_names = []
        for d in s['diffs']:
            kind = 1 if d['characteristic'] in ROTATION else 0
            name = d['env']
            if envs.get(name) != kind:
                fb = (cfg.get('rotation_environment', 'GlassDesertEnvironment') if kind
                      else cfg['environment_fallbacks'].get(name, cfg['default_environment']))
                if envs.get(fb) != kind:
                    raise Fail('fallback environment %s is not in the PS5 build' % fb)
                s['skipped'].append('%s %s: environment %s not on PS5, using %s' % (d['characteristic'], d['name'], name, fb))
                name = fb
            if name not in env_names:
                env_names.append(name)
            d['env_idx'] = env_names.index(name)
        lv_pid, spr_pid, tex_pid, clip_pid = (new_path_id(used, rng) for _ in range(4))
        tex = copy.deepcopy(trees[tmpl_cover_tex])
        set_texture_rgba32(tex, cover_image(s, tex['m_Width']))
        tex['m_Name'] = '%sCover' % s['level_id']
        spr = copy.deepcopy(trees[tmpl_cover_spr])
        spr['m_Name'] = tex['m_Name']
        spr['m_RD']['texture'] = {'m_FileID': 0, 'm_PathID': tex_pid}
        spr['m_RenderDataKey'] = [{'data[%d]' % i: rng.randrange(1 << 32) for i in range(4)}, 21300000]
        start = s['preview_start'] if s['preview_start'] + 1 < s['duration'] else 0.0
        fsb, samples = fsb5_audio(decode_pcm(s['audio'], start, s['preview_len'], fade=True), audio)
        res += b'\0' * ((-len(res)) % 32)
        clip = copy.deepcopy(trees[tmpl_clip])
        clip.update(m_Name='%sPreview' % s['level_id'], m_LoadType=CLIP_FORMAT[audio][1], m_Channels=CHANNELS,
                    m_Frequency=FREQ, m_BitsPerSample=16, m_Length=samples / float(FREQ),
                    m_CompressionFormat=CLIP_FORMAT[audio][0])
        clip['m_Resource'] = {'m_Source': 'archive:/%s/%s.resource' % (cab, cab), 'm_Offset': len(res), 'm_Size': len(fsb)}
        res += fsb
        lv = copy.deepcopy(lt)
        lufs = s['lufs'] if s['lufs'] is not None else lt['_integratedLufs']
        lv.update(m_Name='%sBeatmapLevel' % s['level_id'], _levelID=s['level_id'], _songName=s['name'], _songSubName=s['sub'],
                  _songAuthorName=s['author'], _levelAuthorName=s['mapper'], _beatsPerMinute=s['bpm'], _integratedLufs=lufs,
                  _songTimeOffset=s['offset'], _shuffle=s['shuffle'], _shufflePeriod=s['shuffle_period'],
                  _previewStartTime=start, _previewDuration=s['preview_len'], _songDuration=s['duration'], _contentRating=1)
        lv['_previewAudioClip'] = {'m_FileID': 0, 'm_PathID': clip_pid}
        lv['_coverImage'] = {'m_FileID': 0, 'm_PathID': spr_pid}
        lv['_environmentName'] = {'_environmentName': ''}
        lv['_allDirectionsEnvironmentName'] = {'_environmentName': ''}
        lv['_environmentNames'] = [{'_environmentName': n} for n in env_names]
        lv['_colorSchemes'] = s['schemes']
        lv['_previewDifficultyBeatmapSets'] = []
        for d in s['diffs']:
            sets = lv['_previewDifficultyBeatmapSets']
            if not sets or sets[-1]['_beatmapCharacteristic'] != chars[d['characteristic']]:
                sets.append({'_beatmapCharacteristic': dict(chars[d['characteristic']]), '_previewDifficultyBeatmaps': []})
            sets[-1]['_previewDifficultyBeatmaps'].append({
                '_difficulty': d['value'], '_environmentNameIdx': d['env_idx'], '_beatmapColorSchemeIdx': d['scheme'],
                '_noteJumpMovementSpeed': d['njs'], '_noteJumpStartBeatOffset': d['offset'],
                '_notesCount': d['counts']['notes'], '_obstaclesCount': d['counts']['obstacles'],
                '_bombsCount': d['counts']['bombs'], '_cuttableBeatmapObjectsCount': d['counts']['cuttable']})
        for pid, tree, tmpl in ((tex_pid, tex, tmpl_cover_tex), (spr_pid, spr, tmpl_cover_spr),
                                (clip_pid, clip, tmpl_clip), (lv_pid, lv, tmpl_level)):
            clone_object(sf, tmpl, pid, tree)
            trees[pid], kinds[pid] = tree, kinds[tmpl]
        new_levels.append(lv_pid)
        loud['_perceivedLoudnessPerLevel'].append({'_levelId': s['level_id'], '_perceivedLoudness': lufs, '_checkSum': ''})
    coll['_beatmapLevels'] = [{'m_FileID': 0, 'm_PathID': p} for p in new_levels]

    promo = next(t for t in trees.values() if '_promoBannerInfo' in t)
    promo['m_Name'] = '%s_PackPromoInfo' % PACK_ID
    promo['_levelsPromoInfo'] = []
    binfo = trees[promo['_promoBannerInfo']['m_PathID']]
    binfo['m_Name'] = '%s_PromoInfo' % PACK_ID
    bspr = trees[binfo['_bannerImage']['m_PathID']]
    btex = trees[bspr['m_RD']['texture']['m_PathID']]
    set_texture_rgba32(btex, banner_image(songs, btex['m_Width'], btex['m_Height']))
    btex['m_Name'] = bspr['m_Name'] = '%sPromoBanner' % PACK_ID

    build_credits(sf, trees, kinds, used, rng, ab, songs)

    names = {'.prefab': 'Credits/%sCredits.prefab' % PACK_ID,
             '_PackDefinition.asset': 'SO/%s_PackDefinition.asset' % PACK_ID,
             '_PackPromoInfo.asset': 'SO/%s_PackPromoInfo.asset' % PACK_ID}
    container = []
    for name, info in ab['m_Container']:
        suffix = next(s for s in names if name.endswith(s))
        container.append(['Packages/%s/%s' % (PACK_PKG, names[suffix]), info])
    roots = [info['asset']['m_PathID'] for _, info in container]
    others = {p: t for p, t in trees.items() if p != ab_pid}
    live = reachable(others, roots) | {ab_pid}
    for pid in list(sf.objects):
        if pid not in live:
            del sf.objects[pid]
            trees.pop(pid, None)
    others = {p: t for p, t in trees.items() if p != ab_pid}
    table = []
    for _, info in container:
        pre = preload_for(others, info['asset']['m_PathID'])
        info['preloadIndex'], info['preloadSize'] = len(table), len(pre)
        table += pre
    ab['m_PreloadTable'] = table
    ab['m_Container'] = container

    for k in [k for k in env.file.files if k.startswith(cab) and k != cab]:
        del env.file.files[k]
    streamed = [trees[p]['m_Name'] for p in trees if kinds[p] == 'Texture2D' and trees[p].get('m_StreamData', {}).get('size')]
    if streamed:
        raise Fail('pack template still has streamed textures: %s' % streamed)
    set_resource(env, cab, '.resource', res)
    ab['m_Name'] = ab['m_AssetBundleName'] = hashlib.md5(PACK_FILE.encode()).hexdigest() + '.bundle'
    for pid in trees:
        sf.objects[pid].save_typetree(trees[pid])
    sf.mark_changed()
    out_aa = os.path.join(out_dir, SA, 'aa', 'PS5')
    os.makedirs(out_aa, exist_ok=True)
    dst = os.path.join(out_aa, PACK_FILE)
    save_ps5(env, dst)
    return {'file': os.path.relpath(dst, out_dir), 'size': os.path.getsize(dst), 'cab': cab, 'template_cab': old_cab,
            'characteristics': {n: p['m_PathID'] for n, p in chars.items()},
            'levels': [s['level_id'] for s in songs], 'guids': guids, 'container': [c[0] for c in container]}


# ---------------------------------------------------------------- catalog

def merge_catalog(layers, pack, out_dir):
    src = layers.path(SA + '/aa/catalog.json')
    c = Catalog(src)
    prov_bundle = c.provider_index('UnityEngine.ResourceManagement.ResourceProviders.AssetBundleProvider')
    prov_asset = c.provider_index('UnityEngine.ResourceManagement.ResourceProviders.BundledAssetProvider')
    root = RUNTIME_ROOT + '/PS5/'
    by_id = {c.ids[e[0]]: i for i, e in enumerate(c.entries)}
    if root + PACK_FILE in by_id or any(i.startswith('Packages/%s/' % PACK_PKG) for i in by_id):
        raise Fail('the installed catalog already has the custom pack; build from the installed PKG, not an earlier overlay')
    tmpl_bundle = by_id[next(i for i in by_id if i.startswith(root + TEMPLATE_PACK))]
    tmpl_assets = [i for i, e in enumerate(c.entries) if e[2] >= 0 and c.buckets[e[2]][0] == tmpl_bundle]
    deps = c.buckets[c.entries[tmpl_assets[0]][2]][1:]
    guids = pack['guids']
    # (labels, primary key = the asset's name, GUID): the same key set the stock pack entries have
    keys_for = {'.prefab': (['MusicPackCredits'], PACK_ID + 'Credits', guids['credits'], 'UnityEngine.GameObject'),
                '_PackDefinition.asset': (['PackDefinition'], PACK_ID + '_PackDefinition', guids['def'], 'PackDefinitionSO'),
                '_PackPromoInfo.asset': ([], PACK_ID + '_PackPromoInfo', guids['promo'], 'PackPromoInfoSO')}
    for _, name, guid, _ in keys_for.values():
        for k in (name, guid):
            if c.find_key(k) is not None:
                raise Fail('the catalog already has key %s' % k)
    asm, cls, opts = c.bundle_options(tmpl_bundle)
    size = os.path.getsize(os.path.join(out_dir, SA, 'aa', 'PS5', PACK_FILE))
    opts = dict(opts, m_Hash=PACK_FILE[-len('.bundle') - 32:-len('.bundle')], m_Crc=0, m_BundleSize=size,
                m_BundleName=hashlib.md5(PACK_FILE.encode()).hexdigest())
    be = c.add_entry([c.add_id(root + PACK_FILE), prov_bundle, -1, 0, -1, 0, c.entries[tmpl_bundle][6]])
    c.set_bundle_options(be, asm, cls, opts)
    key_has_ext = c.key(c.entries[tmpl_bundle][5]).endswith('.bundle')
    c.entries[be][5] = c.add_key(PACK_FILE if key_has_ext else PACK_FILE[:-len('.bundle')], [be])
    used = {c.key(k) for k in range(len(c.keys_raw))}
    seed = PACK_FILE
    while True:
        dep_str = str(seeded_rng(seed).randrange(1, 1 << 31))
        if dep_str not in used:
            break
        seed += '_'
    dep_key = c.add_key(dep_str, [be] + deps)
    dep_hash = seeded_rng(dep_str).randrange(-(1 << 31), 1 << 31)
    added = []
    for name in pack['container']:
        suffix = next(s for s in keys_for if name.endswith(s))
        labels, primary, guid, typ = keys_for[suffix]
        keys = labels + [primary, guid]
        ae = c.add_entry([c.add_id(name), prov_asset, dep_key, dep_hash, -1, 0, c.type_index(typ)])
        for k in keys:
            c.add_key(k, [ae])
        c.entries[ae][5] = c.find_key(primary)
        added.append('%s keys=%s' % (name, keys))
    c.check()
    dst = os.path.join(out_dir, SA, 'aa', 'catalog.json')
    with open(dst, 'w', encoding='utf-8') as f:
        f.write(c.serialize())
    return {'base': src, 'bundle': PACK_FILE, 'added': added, 'dependencies': [c.ids[c.entries[d][0]].split('/')[-1] for d in deps]}


# ---------------------------------------------------------------- carried ported packs

def carry_files(installed, extra, out_dir):
    """Copy every StreamingAssets file of the extra layers that the installed game (Layers) lacks or has different."""
    carried = []
    for d in extra:
        for p in sorted(glob.glob(os.path.join(d, SA, '**', '*'), recursive=True)):
            if not os.path.isfile(p):
                continue
            rel = os.path.relpath(p, d)
            if rel == os.path.join(SA, 'aa', 'catalog.json'):
                continue
            inst = installed.path(rel)
            if inst and filecmp.cmp(inst, p, shallow=False):
                continue
            dst = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(p, dst)
            carried.append(rel)
    return carried


# ---------------------------------------------------------------- offline check of the output

def check_all_packs(out_dir, layers):
    """Every pack the new catalog lists: bundle present (overlay or game), its songs present, IDs unique."""
    fails = []
    c = Catalog(os.path.join(out_dir, SA, 'aa', 'catalog.json'))
    pack_ids, level_ids, n = set(), set(), 0
    if c.find_key('PackDefinition') is None:
        return ['catalog: no PackDefinition label'], 0, 0
    for e in c.buckets[c.find_key('PackDefinition')]:
        dep = c.entries[e][2]
        fname = c.ids[c.entries[c.buckets[dep][0]][0]].split('/')[-1]
        rel = os.path.join(SA, 'aa', 'PS5', fname)
        p = os.path.join(out_dir, rel) if os.path.exists(os.path.join(out_dir, rel)) else layers.path(rel)
        if not p:
            fails.append('catalog lists %s, which neither the overlay nor the game has' % fname)
            continue
        n += 1
        for o in UnityPy.load(p).objects:
            if o.type.name != 'MonoBehaviour':
                continue
            try:
                t = o.read_typetree()
            except Exception:
                continue
            if '_packID' in t:
                if t['_packID'] in pack_ids:
                    fails.append('pack ID %s twice' % t['_packID'])
                pack_ids.add(t['_packID'])
            if '_levelID' in t:
                lid = t['_levelID']
                if lid in level_ids:
                    fails.append('level ID %s twice' % lid)
                level_ids.add(lid)
                rel = os.path.join(SA, 'BeatmapLevelsData', lid.lower())
                if not os.path.exists(os.path.join(out_dir, rel)) and not layers.path(rel):
                    fails.append('%s: song file for %s missing' % (fname, lid))
    return fails, n, len(level_ids)


def verify(out_dir, layers, songs, envs, own, max_chart, characteristics):
    fails = []
    fail = fails.append
    cabs = {}
    for p in layers.glob(SA + '/aa/PS5/*.bundle').values():
        for sub, f in serialized_files(UnityPy.load(p)):
            cabs[sub.lower()] = set(f.objects)
    stock_flags = int(UnityPy.load(layers.path('%s/BeatmapLevelsData/%s' % (SA, TEMPLATE_SONG))).file.dataflags)
    level_ids, song_sets, pack_sets = set(), {}, {}
    char_name = {pid: n for n, pid in characteristics.items()}
    for rel in own:
        p = os.path.join(out_dir, rel)
        env = UnityPy.load(p)
        if int(env.file.dataflags) != stock_flags:
            fail('%s: layout flags %#x, stock %#x' % (rel, int(env.file.dataflags), stock_flags))
        (sub, sf), = serialized_files(env)
        plat = getattr(sf, '_m_target_platform', None)
        if plat is None:
            plat = getattr(sf, 'target_platform', None)
        if plat is None or int(plat) != PS5_PLATFORM:
            fail('%s: target platform %s, not PS5 (%d)' % (rel, plat, PS5_PLATFORM))
        ext = [e.path.split('/')[-1].lower() for e in sf.externals]
        res = {k: v for k, v in env.file.files.items() if not hasattr(v, 'objects')}
        trees = {}
        for pid, o in sf.objects.items():
            try:
                trees[pid] = o.read_typetree()
            except Exception as e:
                fail('%s: %s %d unreadable: %s' % (rel, o.type.name, pid, e))
        for pid, t in trees.items():
            for q in iter_pptrs(t):
                if q['m_FileID'] == 0:
                    if q['m_PathID'] and q['m_PathID'] not in sf.objects:
                        fail('%s: %d refers to missing %d' % (rel, pid, q['m_PathID']))
                elif not 0 < q['m_FileID'] <= len(ext):
                    fail('%s: %d refers to external file %d, which is not listed' % (rel, pid, q['m_FileID']))
                elif ext[q['m_FileID'] - 1] not in cabs or q['m_PathID'] not in cabs[ext[q['m_FileID'] - 1]]:
                    fail('%s: external %s:%d does not exist in the game' % (rel, ext[q['m_FileID'] - 1], q['m_PathID']))
        for pid, o in sf.objects.items():
            if pid not in trees:
                continue
            try:
                check_object(rel, sub, o.type.name, trees[pid], res, out_dir, max_chart, envs, char_name,
                             level_ids, song_sets, pack_sets, fail)
            except (KeyError, TypeError, ValueError, IndexError, struct.error) as e:
                fail('%s: %s %d could not be checked (%s: %s)' % (rel, o.type.name, pid, type(e).__name__, e))
        rts = {pid: t for pid, t in trees.items() if sf.objects[pid].type.name == 'RectTransform'}
        for pid, t in rts.items():
            try:
                for ch in t['m_Children']:
                    if ch['m_PathID'] not in rts or rts[ch['m_PathID']]['m_Father']['m_PathID'] != pid:
                        fail('%s: transform hierarchy broken at %d' % (rel, pid))
            except (KeyError, TypeError) as e:
                fail('%s: RectTransform %d could not be checked (%s)' % (rel, pid, e))
    for lid, sets in pack_sets.items():
        if song_sets.get(lid) != sets:
            fail('%s: the pack lists %s, the song file has %s' % (lid, sets, song_sets.get(lid)))
        if any(len(x[1]) != len(set(x[1])) for x in sets):
            fail('%s: a difficulty is listed twice in one characteristic' % lid)
    if level_ids != {s['level_id'] for s in songs}:
        fail('pack levels %s do not match the songs %s' % (sorted(level_ids), sorted(s['level_id'] for s in songs)))
    new = Catalog(os.path.join(out_dir, SA, 'aa', 'catalog.json'))
    new.check()
    old = Catalog(layers.path(SA + '/aa/catalog.json'))
    if new.entries[:len(old.entries)] != old.entries or new.ids[:len(old.ids)] != old.ids or new.keys_raw[:len(old.keys_raw)] != old.keys_raw:
        fail('catalog: existing entries changed')
    for k in range(len(old.keys_raw)):
        if not set(old.buckets[k]) <= set(new.buckets[k]):
            fail('catalog: key %s lost entries' % old.key(k))
    containers = set()
    for rel in own:
        for o in UnityPy.load(os.path.join(out_dir, rel)).objects:
            if o.type.name == 'AssetBundle':
                containers |= {c[0].lower() for c in o.read_typetree()['m_Container']}
    for i in range(len(old.entries), len(new.entries)):
        iid = new.ids[new.entries[i][0]]
        if not iid.endswith('.bundle') and iid.lower() not in containers:
            fail('catalog: new entry %s is not in the pack bundle' % iid)
        if iid.endswith('.bundle') and new.bundle_options(i)[2]['m_BundleSize'] != os.path.getsize(os.path.join(out_dir, SA, 'aa', 'PS5', iid.split('/')[-1])):
            fail('catalog: size of %s' % iid)
    return fails


def check_object(rel, sub, k, t, res, out_dir, max_chart, envs, char_name, level_ids, song_sets, pack_sets, fail):
    """Checks of one object of a file this run built; problems go to fail()."""
    if k == 'AudioClip':
        r = t['m_Resource']
        if not r['m_Source'].startswith('archive:/%s/' % sub) or r['m_Source'].split('/')[-1] not in res:
            fail('%s: clip %s resource %s' % (rel, t['m_Name'], r['m_Source']))
            return
        f = res[r['m_Source'].split('/')[-1]]
        f.seek(r['m_Offset'])
        fsb = bytes(f.read_bytes(r['m_Size']))
        h = parse_fsb5(fsb)
        kind = {FSB_MODE_PCM16: 'pcm', FSB_MODE_VORBIS: 'vorbis'}.get(h['mode'])
        if not (kind and h['total'] == r['m_Size'] and h['stereo'] and h['freq_idx'] == FSB_FREQ[FREQ]
                and abs(h['samples'] / float(FREQ) - t['m_Length']) < 0.01
                and (t['m_CompressionFormat'], t['m_LoadType']) == CLIP_FORMAT[kind]):
            fail('%s: clip %s FSB5 header %s does not match the clip' % (rel, t['m_Name'], {x: h[x] for x in h if x != 'packets'}))
        elif kind == 'vorbis':
            if h.get('vorbis_crc') != VORBIS_SETUP_CRC:
                fail('%s: clip %s names Vorbis setup %s, not FMOD\'s %08x' % (rel, t['m_Name'], h.get('vorbis_crc'), VORBIS_SETUP_CRC))
            elif h['seek'] != vorbis_seek_table(vorbis_positions(h['packets'], *vorbis_modes(vorbis_headers()[0], vorbis_headers()[2])),
                                                 packet_offsets(h['packets']), h['samples']):
                fail('%s: clip %s seek table does not match its packets' % (rel, t['m_Name']))
            else:
                frames, errors = fsb5_vorbis_decode(fsb)
                if errors or frames != h['samples']:
                    fail('%s: clip %s decodes to %d of %d frames%s' % (rel, t['m_Name'], frames, h['samples'],
                                                                     (': ' + errors[0]) if errors else ''))
    elif k == 'TextAsset':
        try:
            j = json.loads(gzip.decompress(t['m_Script'].encode('utf-8', 'surrogateescape')))
        except Exception as e:
            fail('%s: %s does not decode: %s' % (rel, t['m_Name'], e))
            return
        if t['m_Name'].endswith('.audio.gz'):
            if not j.get('bpmData') or j.get('songFrequency') != FREQ:
                fail('%s: %s has no BPM data at %d Hz' % (rel, t['m_Name'], FREQ))
        else:
            v = chart_version(j)
            if v is None or version_tuple(v) > max_chart or version_tuple(v) < (2, 0, 0):
                fail('%s: %s has version %s' % (rel, t['m_Name'], v))
    elif k == 'Texture2D':
        if not (t['m_TextureFormat'] == 4 and not t['m_IsPreProcessed'] and
                len(t['image data']) == rgba32_size(t['m_Width'], t['m_Height'], t['m_MipCount']) == t['m_CompleteImageSize']):
            fail('%s: texture %s' % (rel, t['m_Name']))
    elif k == 'AssetBundle':
        for name, info in t['m_Container']:
            tab = t['m_PreloadTable'][info['preloadIndex']:info['preloadIndex'] + info['preloadSize']]
            if info['asset'] not in tab:
                fail('%s: preload table of %s lacks the asset' % (rel, name))
        if rel.startswith(SA + '/BeatmapLevelsData/'):
            lid = os.path.basename(rel)
            if not any(c[0].endswith('/%sbeatmapleveldata.asset' % lid) for c in t['m_Container']):
                fail('%s: container does not name %sbeatmapleveldata.asset' % (rel, lid))
    elif k == 'MonoBehaviour' and '_difficultyBeatmapSets' in t:
        song_sets[os.path.basename(rel)] = [(st['_beatmapCharacteristicSerializedName'],
                                             [d['_difficulty'] for d in st['_difficultyBeatmaps']]) for st in t['_difficultyBeatmapSets']]
    elif k == 'MonoBehaviour' and '_levelID' in t:
        lid = t['_levelID']
        level_ids.add(lid)
        names = [e['_environmentName'] for e in t['_environmentNames']]
        sets = []
        for st in t['_previewDifficultyBeatmapSets']:
            ch = char_name.get(st['_beatmapCharacteristic']['m_PathID'])
            if ch is None:
                fail('%s: %s uses a characteristic the PS5 game does not have' % (rel, lid))
            sets.append((ch, [d['_difficulty'] for d in st['_previewDifficultyBeatmaps']]))
            for d in st['_previewDifficultyBeatmaps']:
                i = d['_environmentNameIdx']
                if not 0 <= i < len(names) or names[i] not in envs:
                    fail('%s: %s environment index %d of %s' % (rel, lid, i, names))
                elif envs[names[i]] != (1 if ch in ROTATION else 0):
                    fail('%s: %s %s uses environment %s of the wrong kind' % (rel, lid, ch, names[i]))
                if not -1 <= d['_beatmapColorSchemeIdx'] < len(t['_colorSchemes']):
                    fail('%s: %s colour scheme index' % (rel, lid))
        pack_sets[lid.lower()] = sets
        if not os.path.exists(os.path.join(out_dir, SA, 'BeatmapLevelsData', lid.lower())):
            fail('%s: no song file for %s' % (rel, lid))


# ---------------------------------------------------------------- main

def ps5_environments(layers):
    envs = {}
    for p in layers.glob(SA + '/aa/PS5/*environment*_assets_all_*').values():
        for o in UnityPy.load(p).objects:
            if o.type.name == 'MonoBehaviour':
                try:
                    t = o.read_typetree()
                except Exception:
                    continue
                if '_serializedName' in t and '_environmentType' in t:
                    envs[t['_serializedName']] = t['_environmentType']
    return envs


def song_inputs(folder):
    out = []
    for n in sorted(os.listdir(folder)):
        p = os.path.join(folder, n)
        if n.startswith('.'):
            continue
        if (os.path.isfile(p) and n.lower().endswith('.zip')) or (os.path.isdir(p) and any(f.lower() == 'info.dat' for f in os.listdir(p))):
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--pkg', help='the package installed on the console')
    src.add_argument('--base', action='append', help='an extracted game folder (repeatable; later ones win)')
    ap.add_argument('--songs', help='folder of BeatSaver zips / map folders')
    ap.add_argument('--with', dest='with_', help="a porter overlay to carry along (port.py's work/overlay)")
    ap.add_argument('--audio', choices=('vorbis', 'pcm'), default='vorbis',
                    help='song audio: vorbis (as the stock songs, the default) or pcm16 (about 3.5-4x bigger)')
    ap.add_argument('--out', required=True, help='empty output folder for the overlay')
    ap.add_argument('--work', default=os.path.join(HERE, 'work'), help='where --pkg is extracted (default: work/)')
    ap.add_argument('inputs', nargs='*', help='more BeatSaver zips or map folders')
    a = ap.parse_args()
    cfg = load_config()
    max_chart = version_tuple(cfg['max_beatmap_version'])
    out = os.path.realpath(a.out)
    if not shutil.which('ffmpeg'):
        raise Fail('ffmpeg is needed for the audio (brew install ffmpeg)')
    if a.songs and not os.path.isdir(a.songs):
        raise Fail('--songs %s is not a folder' % a.songs)
    inputs = (song_inputs(a.songs) if a.songs else []) + list(a.inputs)
    if not inputs:
        raise Fail('no songs: give --songs <folder> or zip files')
    missing = [i for i in inputs if not os.path.exists(i)]
    if missing:
        raise Fail('not found: %s' % ', '.join(missing))
    if a.pkg and not os.path.isfile(a.pkg):
        raise Fail('--pkg %s is not a file' % a.pkg)
    guarded = (a.base or []) + ([a.with_] if a.with_ else []) + ([os.path.join(a.work, 'installed')] if a.pkg else [])
    for b in guarded:
        b = os.path.realpath(b)
        if out == b or out.startswith(b + os.sep) or b.startswith(out + os.sep):
            raise Fail('--out must be outside, and not contain, %s' % b)
    if os.path.exists(out) and (not os.path.isdir(out) or os.listdir(out)):
        raise Fail('%s is not an empty folder' % out)
    if a.audio == 'vorbis':
        vorbis_preflight()
    bases = [extract_installed(a.pkg, a.work, cfg)] if a.pkg else a.base
    layers = Layers(bases + ([a.with_] if a.with_ else []))
    if not layers.path(SA + '/aa/catalog.json'):
        raise Fail('no Media/StreamingAssets/aa/catalog.json in the game folder')

    songs, refused, seen = [], [], {}
    for p in inputs:
        try:
            s = read_song(p, max_chart)
        except Fail as e:
            refused.append(str(e))
            continue
        lid = s['level_id'].lower()
        if lid in seen:
            refused.append('%s: same BeatSaver map as %s (%s); left out' % (os.path.basename(p), os.path.basename(seen[lid]), s['key']))
        elif layers.path('%s/BeatmapLevelsData/%s' % (SA, lid)):
            refused.append('%s: level %s already exists in the game; left out' % (os.path.basename(p), s['level_id']))
        else:
            seen[lid] = p
            songs.append(s)
    for r in refused:
        log('  left out: %s' % r)
    if not songs:
        raise Fail('none of the %d maps can be used' % len(inputs))
    songs.sort(key=lambda s: (s['name'].lower(), s['key']))
    envs = ps5_environments(layers)
    os.makedirs(out, exist_ok=True)
    report = {'base': bases, 'audio': a.audio, 'songs': [], 'left_out': refused}
    built = []
    for s in songs:
        log('song %-16s %s - %s [%s]' % (s['level_id'], s['author'], s['name'],
                                          ', '.join('%s %s' % (d['characteristic'], d['name']) for d in s['diffs'])))
        try:
            report['songs'].append(build_song_bundle(layers, s, out, a.audio))
            built.append(s)
        except Fail as e:
            refused.append('%s: %s' % (os.path.basename(s['path'].rstrip(os.sep)), e))
            log('  left out: %s' % refused[-1])
            partial = os.path.join(out, SA, 'BeatmapLevelsData', s['level_id'].lower())
            if os.path.exists(partial):
                os.remove(partial)
    songs = built
    if not songs:
        raise Fail('none of the %d maps can be used' % len(inputs))
    report['pack'] = build_pack_bundle(layers, songs, envs, cfg, out, a.audio)   # adds environment notes to s['skipped']
    report['catalog'] = merge_catalog(layers, report['pack'], out)
    own = [report['pack']['file']] + [r['file'] for r in report['songs']]
    if a.with_:
        report['carried'] = carry_files(Layers(bases), [a.with_], out)
        log('carried %d files from %s' % (len(report['carried']), a.with_))
    log('checking the output ...')
    fails = verify(out, layers, songs, envs, own, max_chart, report['pack']['characteristics'])
    more, npacks, nlevels = check_all_packs(out, layers)
    fails += more
    report['packs_in_catalog'], report['levels_in_catalog'] = npacks, nlevels
    log('catalog: %d packs, %d levels' % (npacks, nlevels))
    report['check'] = fails or 'ok'
    total = sum(os.path.getsize(p) for p in glob.glob(os.path.join(out, '**', '*'), recursive=True) if os.path.isfile(p))
    report['overlay_bytes'] = total
    with open(out.rstrip(os.sep) + '.report.json', 'w') as f:
        json.dump(report, f, indent=1)
    for s in songs:
        for note in s['skipped']:
            log('  note %s: %s' % (s['level_id'], note))
    if fails:
        for f in fails:
            log('FAIL', f)
        raise Fail('%d problems in the output; nothing should be uploaded' % len(fails))
    log('%d songs (%d left out), overlay %.1f MB in %s (report: %s.report.json)'
        % (len(songs), len(refused), total / 1e6, out, out.rstrip(os.sep)))
    log('upload the CONTENTS of that folder to backports/PPSA15035/ in the console folder that holds the package,')
    log('e.g. /mnt/ext1/etaHEN/games/backports/PPSA15035/ (see docs/CUSTOM-SONGS.md)')


if __name__ == '__main__':
    try:
        main()
    except Fail as e:
        print('error: %s' % e, file=sys.stderr)
        sys.exit(1)

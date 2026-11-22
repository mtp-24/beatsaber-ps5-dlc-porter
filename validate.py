"""Independent checks of the built overlay against the original PS5 game files.

usage: validate.py <ps5_game_dir> <overlay_dir> <report.json> <covers.png>
"""
import glob
import gzip
import os
import re
import struct
import sys

import UnityPy
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from catalog_io import Catalog  # noqa: E402

UnityPy.config.FALLBACK_UNITY_VERSION = '2022.3.33f1'
FAIL = []


def fail(msg):
    FAIL.append(msg); print('FAIL', msg, flush=True)


def ok(msg):
    print('ok  ', msg, flush=True)


def sf_header(blob):
    """Independent SerializedFile header parse (version >= 22)."""
    ver = struct.unpack_from('>I', blob, 8)[0]
    p = 48
    end = blob.index(b'\0', p)
    uver = blob[p:end].decode(); p = end + 1
    endian = '<' if blob[16] == 0 else '>'
    plat = struct.unpack_from(endian + 'I', blob, p)[0]
    return ver, uver, plat, blob[p + 4]


def rgba_size(w, h, mips):
    n = 0
    for _ in range(mips):
        n += w * h * 4; w, h = max(1, w // 2), max(1, h // 2)
    return n


def bundle_files(path):
    env = UnityPy.load(path)
    out = []
    for f in env.files.values():
        for sub, sf in getattr(f, 'files', {}).items():
            if hasattr(sf, 'objects'):
                out.append((sub, sf, f))
    return env, out


def version_tuple(v):
    return tuple(int(x) for x in v.split('.'))


def main():
    ps5_dir, overlay, report_path, sheet_path = sys.argv[1:5]
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'analysis'))
    from meta import Meta
    import json
    report = json.load(open(report_path))
    unity, max_ver = report['ps5_unity_version'], report['max_beatmap_version']
    m = Meta(os.path.join(ps5_dir, 'Media/Metadata/global-metadata.dat'))
    known_scripts = set()
    for ti in range(len(m.types)):
        t = m.types[ti]
        known_scripts.add((m.s(t[1]), m.s(t[0]), m.image_name(m.type_image[ti])[:-4]))

    ps5_aa = os.path.join(ps5_dir, 'Media/StreamingAssets/aa/PS5')
    ov_aa = os.path.join(overlay, 'Media/StreamingAssets/aa/PS5')
    cab_objs, cab_bundle, bundle_cabs = {}, {}, {}
    for b in sorted(f for f in set(os.listdir(ps5_aa)) | set(os.listdir(ov_aa)) if f.endswith('.bundle')):
        path = os.path.join(ov_aa, b) if os.path.exists(os.path.join(ov_aa, b)) else os.path.join(ps5_aa, b)
        _, files = bundle_files(path)
        for sub, sf, _f in files:
            cab_objs[sub.lower()] = sf.objects
            cab_bundle[sub.lower()] = b
            bundle_cabs[b] = [e.path.split('/')[-1].lower() for e in sf.externals]

    sprites = []
    converted = tuple(report.get('converted_packs', []))
    shipped = sorted(glob.glob(os.path.join(ov_aa, '*.bundle'))) + sorted(glob.glob(os.path.join(overlay, 'Media/StreamingAssets/BeatmapLevelsData/*')))
    bad_layout = []
    for path in shipped:
        raw = open(path, 'rb').read(128)
        p = raw.index(b'\0') + 1 + 4
        p = raw.index(b'\0', p) + 1
        p = raw.index(b'\0', p) + 1
        flags = struct.unpack_from('>I', raw, p + 16)[0]
        if flags != 0x243:
            bad_layout.append('%s=0x%x' % (os.path.basename(path), flags))
    if bad_layout:
        fail('bundles not in the stock PS5 layout (flags 0x243): %s' % bad_layout)
    else:
        ok('all %d shipped bundles use the stock PS5 layout (flags 0x243)' % len(shipped))
    for path in sorted(glob.glob(os.path.join(ov_aa, '*.bundle'))):
        name = os.path.basename(path)
        if name.startswith(converted):
            continue
        orig_path = os.path.join(ps5_aa, name)
        if not os.path.exists(orig_path):
            fail('%s: unexpected new bundle' % name); continue
        _, new_files = bundle_files(path)
        _, old_files = bundle_files(orig_path)
        nf = {sub: f for sub, _, f in new_files}
        of = {sub: f for sub, _, f in old_files}
        if set(nf) != set(of):
            fail('%s: bundle file list changed' % name); continue
        for sub, f in nf.items():
            if set(f.files) != set(of[sub].files):
                fail('%s: bundle nodes changed' % name)
            for node in f.files:
                if not node.startswith('CAB-') or '.' in node:
                    a, b = f.files[node], of[sub].files[node]
                    if bytes(a.bytes if hasattr(a, 'bytes') else a.read()) != bytes(b.bytes if hasattr(b, 'bytes') else b.read()):
                        fail('%s: resource node %s changed' % (name, node))
        (sub, nsf, _), = new_files
        osf = [s for su, s, _ in old_files if su == sub][0]
        if set(nsf.objects) != set(osf.objects) or nsf._m_target_platform != osf._m_target_platform:
            fail('%s: object set or platform changed' % name); continue
        changed = []
        for pid, o in nsf.objects.items():
            if o.get_raw_data() != osf.objects[pid].get_raw_data():
                changed.append(pid)
        if len(changed) != 1:
            fail('%s: %d objects changed (expected exactly the pack definition)' % (name, len(changed))); continue
        a, b = nsf.objects[changed[0]].read_typetree(), osf.objects[changed[0]].read_typetree()
        diff = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
        if diff != {'_tags'} or (b['_tags'], a['_tags']) != (2, 3):
            fail('%s: pack definition diff %s tags %s -> %s' % (name, diff, b.get('_tags'), a.get('_tags')))
        else:
            ok('%s: identical to PS5 original except %s _tags 2 -> 3' % (name, a['m_Name']))

    targets = [(p, 'pack') for p in sorted(glob.glob(os.path.join(ov_aa, '*.bundle'))) if os.path.basename(p).startswith(converted)]
    targets += [(p, 'song') for p in sorted(glob.glob(os.path.join(overlay, 'Media/StreamingAssets/BeatmapLevelsData/*')))]
    for path, kind in targets:
        name = os.path.basename(path)
        raw = open(path, 'rb').read()
        if not raw.startswith(b'UnityFS\0') or unity.encode() + b'\0' not in raw[:64]:
            fail('%s: bundle header not UnityFS %s' % (name, unity))
        env, files = bundle_files(path)
        for sub, sf, f in files:
            ver, uver, plat, tt = sf_header(f.files[sub].bytes if hasattr(f.files[sub], 'bytes') else sf.reader.bytes)
            if (uver, plat, tt) != (unity, 44, 1):
                fail('%s: serialized header %s plat %d typetree %d' % (name, uver, plat, tt))
            exts = [e.path.split('/')[-1].lower() for e in sf.externals]
            bad_refs, n_refs = [], 0
            trees = {}
            for pid, o in sf.objects.items():
                try:
                    trees[pid] = o.read_typetree()
                except Exception as e:
                    fail('%s: object %d (%s) unreadable: %s' % (name, pid, o.type.name, e)); continue
            for pid, t in trees.items():
                o = sf.objects[pid]
                stack = [t]
                while stack:
                    x = stack.pop()
                    if isinstance(x, dict):
                        if set(x) == {'m_FileID', 'm_PathID'}:
                            if x['m_PathID'] == 0:
                                continue
                            n_refs += 1
                            if x['m_FileID'] == 0:
                                if x['m_PathID'] not in sf.objects:
                                    bad_refs.append(('local', x['m_PathID']))
                            else:
                                cab = exts[x['m_FileID'] - 1]
                                if cab not in cab_objs or x['m_PathID'] not in cab_objs[cab]:
                                    bad_refs.append((cab, x['m_PathID']))
                        else:
                            stack.extend(x.values())
                    elif isinstance(x, list):
                        stack.extend(x)
                if o.type.name == 'MonoScript':
                    k = (t['m_Namespace'], t['m_ClassName'], t['m_AssemblyName'].replace('.dll', ''))
                    if k not in known_scripts:
                        fail('%s: script %s not in PS5 build' % (name, k))
                if o.type.name == 'Texture2D':
                    if t.get('m_IsPreProcessed'):
                        fail('%s: %s has m_IsPreProcessed set (PS5 would treat its data as GPU-tiled)' % (name, t['m_Name']))
                    if t['m_TextureFormat'] != 4:
                        fail('%s: %s is texture format %d, not linear RGBA32' % (name, t['m_Name'], t['m_TextureFormat']))
                    else:
                        exp = rgba_size(t['m_Width'], t['m_Height'], t['m_MipCount'])
                        have = t['m_StreamData']['size'] or len(t['image data'])
                        if exp != t['m_CompleteImageSize'] or exp != have:
                            fail('%s: %s RGBA32 size %d/%d expected %d' % (name, t['m_Name'], t['m_CompleteImageSize'], have, exp))
                if o.type.name == 'Sprite' and kind == 'pack':
                    sprites.append((name, t['m_Name'], o))
                if o.type.name == 'MonoBehaviour' and '_levelID' in t:
                    envs = [e for e in t.get('_environmentNames', [])] + [t[k] for k in ENV_FIELDS if isinstance(t.get(k), dict)]
                    for e in envs:
                        if e.get('_environmentName') and e['_environmentName'] not in ENVS:
                            fail('%s: %s uses environment %s' % (name, t['_levelID'], e['_environmentName']))
                if o.type.name == 'TextAsset' and kind == 'song':
                    data = t['m_Script'].encode('utf-8', 'surrogateescape') if isinstance(t['m_Script'], str) else bytes(t['m_Script'])
                    if data[:2] == b'\x1f\x8b':
                        data = gzip.decompress(data)
                    mv = re.search(rb'"_?version"\s*:\s*"(\d+(?:\.\d+)*)"', data[:512])  # v2 charts: "_version"
                    if mv is None:
                        fail('%s: %s has no format version' % (name, t['m_Name']))
                    elif version_tuple(mv.group(1).decode()) > version_tuple(max_ver):
                        fail('%s: %s is format %s, newer than the PS5 build reads (%s)' % (name, t['m_Name'], mv.group(1).decode(), max_ver))
                if o.type.name == 'AssetBundle':
                    table = t['m_PreloadTable']
                    for cname, info in t['m_Container']:
                        if info['preloadIndex'] < 0 or info['preloadIndex'] + info['preloadSize'] > len(table):
                            fail('%s: preload range out of table for %s' % (name, cname))
                    if kind == 'song':
                        lid = os.path.basename(path)
                        if not any(c[0].endswith('/' + lid + 'beatmapleveldata.asset') for c in t['m_Container']):
                            fail('%s: container lacks %sbeatmapleveldata.asset: %s' % (name, lid, [c[0] for c in t['m_Container']]))
            if bad_refs:
                fail('%s: %d of %d references unresolved, e.g. %s' % (name, len(bad_refs), n_refs, bad_refs[:4]))
            else:
                ok('%s: %d objects, %d references all resolve' % (name, len(sf.objects), n_refs))

    tiles = []
    for bname, sname, o in sprites:
        try:
            tiles.append(o.read().image.convert('RGBA').resize((96, 96)))
        except Exception as e:
            fail('%s: sprite %s does not render: %s' % (bname, sname, e))
    if tiles:
        cols = 10
        sheet = Image.new('RGBA', (cols * 96, ((len(tiles) + cols - 1) // cols) * 96), (40, 40, 40, 255))
        for i, im in enumerate(tiles):
            sheet.paste(im, ((i % cols) * 96, (i // cols) * 96))
        sheet.save(sheet_path)
        ok('%d sprites rendered to %s' % (len(tiles), sheet_path))

    orig = Catalog(os.path.join(ps5_dir, 'Media/StreamingAssets/aa/catalog.json'))
    new = Catalog(os.path.join(overlay, 'Media/StreamingAssets/aa/catalog.json'))
    new.check()
    n0 = len(orig.entries)
    if new.ids[:len(orig.ids)] != orig.ids:
        fail('catalog: original internal ids changed')
    if new.providers != orig.providers or new.types != orig.types:
        fail('catalog: provider or resource type tables changed')
    for i in range(n0):
        a, b = orig.entries[i], new.entries[i]
        if a[:4] + a[5:] != b[:4] + b[5:]:
            fail('catalog: original entry %d changed %s -> %s' % (i, a, b))
        if a[4] != b[4]:
            fname = orig.ids[a[0]].split('/')[-1]
            if not os.path.exists(os.path.join(ov_aa, fname)):
                fail('catalog: extra data of entry %d changed but bundle not in overlay' % i)
            else:
                _, _, opts = new.bundle_options(i)
                _, _, oopts = orig.bundle_options(i)
                if opts['m_Crc'] != 0 or opts['m_BundleSize'] != os.path.getsize(os.path.join(ov_aa, fname)) or {k: v for k, v in opts.items() if k not in ('m_Crc', 'm_BundleSize')} != {k: v for k, v in oopts.items() if k not in ('m_Crc', 'm_BundleSize')}:
                    fail('catalog: options of modified bundle %s wrong: %s' % (fname, opts))
    for fname in os.listdir(ov_aa):
        e = [i for i, x in enumerate(new.entries) if new.ids[x[0]].endswith('/' + fname)]
        if len(e) != 1 or new.bundle_options(e[0])[2]['m_Crc'] != 0:
            fail('catalog: overlay bundle %s has %d entries or non-zero CRC' % (fname, len(e)))
    for k in range(len(orig.keys_raw)):
        if orig.keys_raw[k] != new.keys_raw[k] or not set(orig.buckets[k]) <= set(new.buckets[k]):
            fail('catalog: original key %d changed' % k)
    root = '{UnityEngine.AddressableAssets.Addressables.RuntimePath}/PS5/'
    on_disk = set(os.listdir(ps5_aa)) | set(os.listdir(ov_aa))
    for i, e in enumerate(new.entries):
        iid = new.ids[e[0]]
        if iid.startswith(root) and iid[len(root):] not in on_disk:
            fail('catalog: bundle entry %d file missing: %s' % (i, iid))
    label = new.find_key('PackDefinition')
    packdefs = new.buckets[label]
    ok('PackDefinition label: %d entries (original %d)' % (len(packdefs), len(orig.buckets[orig.find_key('PackDefinition')])))
    container_cache = {}

    def container(bundle_file):
        if bundle_file not in container_cache:
            path = os.path.join(ov_aa, bundle_file) if os.path.exists(os.path.join(ov_aa, bundle_file)) else os.path.join(ps5_aa, bundle_file)
            names = set()
            for o in UnityPy.load(path).objects:
                if o.type.name == 'AssetBundle':
                    names |= {c[0].lower() for c in o.read_typetree()['m_Container']}
            container_cache[bundle_file] = names
        return container_cache[bundle_file]

    shipped_bundles = set(os.listdir(ov_aa))
    for e in range(len(new.entries)):
        iid, pr, dk, dh, di, pk, rt = new.entries[e]
        if e < n0 and (dk < 0 or new.ids[new.entries[new.buckets[dk][0]][0]][len(root):] not in shipped_bundles):
            continue
        if new.providers[pr].endswith('AssetBundleProvider'):
            _, _, opts = new.bundle_options(e)
            if opts['m_Crc'] != 0:
                fail('catalog: new bundle entry %d has CRC %d' % (e, opts['m_Crc']))
            size = os.path.getsize(os.path.join(ov_aa, new.ids[iid][len(root):]))
            if opts['m_BundleSize'] != size:
                fail('catalog: bundle size mismatch %s' % new.ids[iid])
            continue
        deps = new.buckets[dk]
        first = new.ids[new.entries[deps[0]][0]][len(root):]
        if new.ids[iid].lower() not in container(first):
            fail('catalog: asset %s not in container of %s' % (new.ids[iid], first))
        dep_files = set()
        for d in deps:
            if not new.providers[new.entries[d][1]].endswith('AssetBundleProvider'):
                fail('catalog: dependency %d of %d is not a bundle' % (d, e))
            dep_files.add(new.ids[new.entries[d][0]][len(root):])
        need = {cab_bundle[c] for c in bundle_cabs.get(first, []) if c in cab_bundle}
        if need - dep_files:
            fail('catalog: %s depends on %s but its dependency list lacks them' % (new.ids[iid], sorted(need - dep_files)))
        ok('catalog: %s -> %s [%s] keys=%s' % (new.ids[iid].split('/')[-1], first[:30], new.types[rt], new.keys_of(e)))

    pack_ids, level_ids = {}, {}
    pack_files = sorted({new.ids[new.entries[new.buckets[new.entries[e][2]][0]][0]][len(root):] for e in packdefs})
    for fname in pack_files:
        path = os.path.join(ov_aa, fname) if fname in shipped_bundles else os.path.join(ps5_aa, fname)
        for o in UnityPy.load(path).objects:
            if o.type.name != 'MonoBehaviour':
                continue
            t = o.read_typetree()
            if '_levelProductPacks' in t and report.get('unlock_all_dlc') and (t.get('_tags', 0) & 3) == 2:
                fail('%s: DLC-only pack (_tags %s) stays locked although unlock_all_dlc is on' % (fname, t.get('_tags')))
            if '_packID' in t:
                if t['_packID'] in pack_ids:
                    fail('pack ID %s is in both %s and %s' % (t['_packID'], pack_ids[t['_packID']], fname))
                pack_ids[t['_packID']] = fname
            if '_levelID' in t:
                if t['_levelID'] in level_ids and level_ids[t['_levelID']] != fname:
                    fail('level ID %s is in both %s and %s' % (t['_levelID'], level_ids[t['_levelID']], fname))
                level_ids[t['_levelID']] = fname
    ok('%d packs, %d levels: no duplicate IDs%s' % (len(pack_ids), len(level_ids), ', no locked DLC pack' if report.get('unlock_all_dlc') else ''))
    guids = set()
    for p in glob.glob(os.path.join(ov_aa, '*.bundle')):
        for o in UnityPy.load(p).objects:
            if o.type.name == 'MonoBehaviour':
                stack = [o.read_typetree()]
                while stack:
                    x = stack.pop()
                    if isinstance(x, dict):
                        if x.get('m_AssetGUID'):
                            guids.add(x['m_AssetGUID'])
                        stack.extend(x.values())
                    elif isinstance(x, list):
                        stack.extend(x)
    for g in sorted(guids):
        k = new.find_key(g)
        if k is None:
            fail('catalog: AssetReference GUID %s has no catalog key' % g)
    ok('%d AssetReference GUIDs in the packs all have catalog keys' % len(guids))
    print('\nRESULT:', 'FAILED (%d)' % len(FAIL) if FAIL else 'ALL CHECKS PASSED')
    sys.exit(1 if FAIL else 0)


ENVS = set()
ENV_FIELDS = ('_environmentName', '_allDirectionsEnvironmentName')

if __name__ == '__main__':
    ps5 = sys.argv[1]
    for p in glob.glob(os.path.join(ps5, 'Media/StreamingAssets/aa/PS5/*environment*_assets_all_*')):
        for o in UnityPy.load(p).objects:
            if o.type.name == 'MonoBehaviour':
                try:
                    t = o.read_typetree()
                except Exception:
                    continue
                if '_serializedName' in t and '_environmentType' in t:
                    ENVS.add(t['_serializedName'])
    main()

#!/usr/bin/env python3
"""Port Beat Saber PC music packs that the PS5 version lacks into a PS5 fake package.

    python port.py --ps5-pkg "Beat Saber (v01.039.000).pkg" --pc-game "Beat Saber" --work work

Steps (each writes into --work, see README.md):
  1. extract the original PS5 package with PS5 Dump Forge (once; reused while it exists)
  2. find PC packs that are new to the PS5 build, or that hold more levels than the PS5 copy
  3. convert those pack bundles and their song files for the PS5 player and fix what the PS5 runtime needs
  4. optionally make every DLC pack always owned (no dlc_emu.ini needed)
  5. merge the new entries into the original PS5 Addressables catalog
  6. validate everything offline (validate.py)
  7. assemble the full game folder and pack it into a debug PKG with Dump Forge
"""
import argparse
import copy
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import attr
import UnityPy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from catalog_io import Catalog  # noqa: E402
from unity_bundles import (RGBA32, load_as_ps5, new_path_id, save_ps5, seeded_rng,  # noqa: E402
                           serialized_files, set_texture_rgba32, walk_pptrs)

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_RE = re.compile(r'^(.+?)_([0-9a-f]{32})\.bundle$')
RUNTIME_ROOT = '{UnityEngine.AddressableAssets.Addressables.RuntimePath}'
TAGS_BUILTIN_AND_DLC = 3
BUILTIN_EXTERNALS = {'unity default resources', 'unity_builtin_extra'}
ENV_FIELDS = ('_environmentName', '_allDirectionsEnvironmentName')


def log(*a):
    print(*a, flush=True)


def bundle_base(fname):
    m = BUNDLE_RE.match(fname)
    return m.group(1) if m else fname


class Ps5Game:
    def __init__(self, root):
        self.root = root
        self.aa = os.path.join(root, 'Media/StreamingAssets/aa')
        self.bundles = os.path.join(self.aa, 'PS5')
        self.levels = os.path.join(root, 'Media/StreamingAssets/BeatmapLevelsData')
        self.catalog = Catalog(os.path.join(self.aa, 'catalog.json'))
        self.unity_version = UnityPy.load(glob.glob(os.path.join(self.bundles, '*_monoscripts_*.bundle'))[0]).file.version_engine


class PcGame:
    def __init__(self, root):
        self.root = root
        self.aa = os.path.join(root, 'Beat Saber_Data/StreamingAssets/aa')
        self.bundles = os.path.join(self.aa, 'StandaloneWindows64')
        self.catalog = Catalog(os.path.join(self.aa, 'catalog.json'))
        self.song_dirs = [os.path.join(root, 'DLC/Levels'), os.path.join(root, 'Beat Saber_Data/StreamingAssets/BeatmapLevelsData')]

    def find_song(self, level_id):
        want = level_id.lower()
        for d in self.song_dirs:
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                p = os.path.join(d, name)
                if name.lower() != want:
                    continue
                if os.path.isfile(p):
                    return p
                files = sorted(f for f in os.listdir(p) if not f.startswith('.'))
                exact = [f for f in files if f.lower() == want]
                pick = exact or [f for f in files if not os.path.splitext(f)[1]] or files
                if pick:
                    return os.path.join(p, pick[0])
        return None


def pack_bundles(cat, sep):
    """Sorted bundle file names of every pack definition under the PackDefinition label."""
    out = set()
    for e in cat.buckets[cat.find_key('PackDefinition')]:
        first = cat.entries[cat.buckets[cat.entries[e][2]][0]][0]
        out.add(cat.ids[first].split(sep)[-1])
    return sorted(out)


def bundle_assets(cat, bundle_fname, sep):
    """Asset entries whose first dependency is this bundle (the pack definition, promo info, credits prefab, ...)."""
    out = []
    for i, e in enumerate(cat.entries):
        if e[2] < 0:
            continue
        deps = cat.buckets[e[2]]
        if deps and cat.ids[cat.entries[deps[0]][0]].split(sep)[-1] == bundle_fname:
            out.append(i)
    return out


def read_monobehaviours(path):
    env = UnityPy.load(path)
    out = []
    for o in env.objects:
        if o.type.name == 'MonoBehaviour':
            try:
                out.append(o.read_typetree())
            except Exception:
                pass
    return out


def pack_summary(path):
    """(pack definition tree, pack ID, set of level IDs) of a pack bundle."""
    trees = read_monobehaviours(path)
    packdef = next((t for t in trees if '_levelProductPacks' in t and '_tags' in t), None)
    pack_id = next((t['_packID'] for t in trees if '_packID' in t), None)
    return packdef, pack_id, {t['_levelID'] for t in trees if '_levelID' in t}


def ps5_environments(ps5):
    envs = set()
    for p in glob.glob(os.path.join(ps5.bundles, '*environment*_assets_all_*.bundle')):
        for t in read_monobehaviours(p):
            if '_serializedName' in t and '_environmentType' in t:
                envs.add(t['_serializedName'])
    return envs


def known_scripts(metadata_path):
    sys.path.insert(0, os.path.join(HERE, 'analysis'))
    from meta import Meta
    m = Meta(metadata_path)
    out = {}
    for ti in range(len(m.types)):
        t = m.types[ti]
        out.setdefault((m.s(t[1]), m.s(t[0])), set()).add(m.image_name(m.type_image[ti])[:-4])
    return out


def ps5_cab_index(ps5):
    """({CAB name: object path IDs}, {CAB name: bundle file name}) for every PS5 bundle."""
    objs, where = {}, {}
    for b in sorted(os.listdir(ps5.bundles)):
        if not b.endswith('.bundle'):
            continue
        for sub, sf in serialized_files(UnityPy.load(os.path.join(ps5.bundles, b))):
            objs[sub.lower()] = set(sf.objects.keys())
            where[sub.lower()] = b
    return objs, where


def discover(ps5, pc, cfg, report):
    """Decide which PC packs to port, matching packs by pack ID.

    'new': the PS5 build has no pack with that ID. 'update': the PC pack holds every PS5 level plus more; its
    bundle replaces the PS5 one, which loses the PS5 store and leaderboard data of the levels it already had.
    """
    ps5_by_id = {}
    for fname in pack_bundles(ps5.catalog, '/'):
        _, pack_id, ids = pack_summary(os.path.join(ps5.bundles, fname))
        if pack_id is None:
            raise SystemExit('PS5 pack bundle %s has no readable pack ID' % fname)
        ps5_by_id[pack_id] = (fname, ids)
    plan, warnings = [], report.setdefault('warnings', [])
    pc_fnames = pack_bundles(pc.catalog, '\\')
    for name in sorted(set(cfg['skip_packs']) - {bundle_base(f) for f in pc_fnames}):
        warnings.append('skip_packs entry %r matches no PC pack bundle (names look like "coldplay_pack_assets_all")' % name)
    for pc_fname in pc_fnames:
        base = bundle_base(pc_fname)
        if base in cfg['skip_packs']:
            continue
        packdef, pack_id, ids = pack_summary(os.path.join(pc.bundles, pc_fname))
        if packdef is None or pack_id is None or (packdef['_tags'] & 3) == 0 or packdef['_tags'] & 4:
            continue
        if pack_id not in ps5_by_id:
            plan.append(dict(kind='new', base=base, pack_id=pack_id, pc_fname=pc_fname, out_fname=pc_fname, levels=sorted(ids)))
            continue
        ps5_fname, ps5_ids = ps5_by_id[pack_id]
        if ids == ps5_ids or ids < ps5_ids:
            continue
        if not ids > ps5_ids:
            warnings.append('%s: PC and PS5 level sets differ both ways; not ported (PC extra %s, PS5 extra %s)'
                            % (base, sorted(ids - ps5_ids), sorted(ps5_ids - ids)))
            continue
        if not cfg['unlock_all_dlc'] and (packdef['_tags'] & 3) == 2:
            warnings.append('%s: not updated: the PC bundle has no PS5 store data, so with unlock_all_dlc off the levels '
                            'already owned on PS5 (%s) would lock' % (base, ', '.join(sorted(ps5_ids))))
            continue
        warnings.append('%s: updated; its existing PS5 levels (%s) lose their PS5 leaderboards' % (base, ', '.join(sorted(ps5_ids))))
        plan.append(dict(kind='update', base=base, pack_id=pack_id, pc_fname=pc_fname, out_fname=ps5_fname, levels=sorted(ids),
                         existing=sorted(ps5_ids)))
    report['plan'] = [{k: v for k, v in p.items() if k != 'existing'} for p in plan]
    return plan


def fix_pack_bundle(item, ps5, pc, cfg, scripts, envs, ps5_cabs, pc_shared, out_dir, report):
    env = load_as_ps5(os.path.join(pc.bundles, item['pc_fname']), ps5.unity_version)
    (sub, sf), = serialized_files(env)
    ext = [e.path.split('/')[-1].lower() for e in sf.externals]
    used = set(sf.objects.keys())
    trees = {pid: o.read_typetree() for pid, o in sf.objects.items()}
    dirty = set()
    r = report.setdefault('packs', {}).setdefault(item['base'], {'kind': item['kind'], 'bundle': item['out_fname']})
    rng = seeded_rng('covers', item['out_fname'])

    for pid, o in sf.objects.items():
        t = trees[pid]
        kind = o.type.name
        if kind == 'MonoScript':
            have = scripts.get((t['m_Namespace'], t['m_ClassName']))
            if not have:
                raise SystemExit('%s: script %s.%s does not exist in the PS5 build' % (item['base'], t['m_Namespace'], t['m_ClassName']))
            if t['m_AssemblyName'].replace('.dll', '') not in have:
                if len(have) > 1:
                    raise SystemExit('%s: script %s.%s is in several PS5 assemblies (%s); add the right one by hand'
                                     % (item['base'], t['m_Namespace'], t['m_ClassName'], ', '.join(sorted(have))))
                fix = next(iter(have))
                r.setdefault('assembly_fixed', []).append('%s: %s -> %s' % (t['m_ClassName'], t['m_AssemblyName'], fix))
                t['m_AssemblyName'] = fix
                dirty.add(pid)
        elif kind == 'MonoBehaviour':
            if cfg['unlock_all_dlc'] and '_levelProductPacks' in t and '_tags' in t and t['_tags'] == 2:
                r['tags'] = '2 -> %d' % TAGS_BUILTIN_AND_DLC
                t['_tags'] = TAGS_BUILTIN_AND_DLC
                dirty.add(pid)
            if '_levelID' in t:
                fields = [e for e in t.get('_environmentNames', [])] + [t[k] for k in ENV_FIELDS if isinstance(t.get(k), dict)]
                if not any(k in t for k in ('_environmentNames',) + ENV_FIELDS):
                    raise SystemExit('%s: level %s has none of the known environment fields; the PC level format changed'
                                     % (item['base'], t['_levelID']))
                for e in fields:
                    name = e.get('_environmentName', '')
                    if name and name not in envs:
                        new = cfg['environment_fallbacks'].get(name, cfg['default_environment'])
                        if new not in envs:
                            raise SystemExit('fallback environment %s for %s is not in the PS5 build' % (new, name))
                        r.setdefault('environments', []).append('%s: %s -> %s' % (t['_levelID'], name, new))
                        e['_environmentName'] = new
                        dirty.add(pid)
        elif kind == 'Texture2D' and (t['m_TextureFormat'] != RGBA32 or t.get('m_IsPreProcessed')):
            img = o.read().image
            r.setdefault('textures_reencoded', []).append('%s fmt %d %dx%d -> RGBA32' % (t['m_Name'], t['m_TextureFormat'], t['m_Width'], t['m_Height']))
            set_texture_rgba32(t, img)
            dirty.add(pid)

    unresolved = sorted(c for c in ext if c not in ps5_cabs and c not in BUILTIN_EXTERNALS)
    if unresolved:
        raise SystemExit('%s: depends on bundles the PS5 build does not have: %s' % (item['base'], unresolved))
    item['externals'] = sorted(c for c in ext if c in ps5_cabs)
    missing = {}
    for t in trees.values():
        def collect(p):
            if p['m_FileID'] > 0:
                cab = ext[p['m_FileID'] - 1]
                if cab in ps5_cabs and p['m_PathID'] not in ps5_cabs[cab]:
                    missing[(p['m_FileID'], p['m_PathID'])] = cab
        walk_pptrs(t, collect)

    if missing:
        tmpl_sprite = next((pid for pid, o in sf.objects.items() if o.type.name == 'Sprite'
                            and trees[pid]['m_RD']['texture']['m_FileID'] == 0
                            and trees[pid]['m_Rect']['width'] == trees[pid]['m_Rect']['height'] == trees[pid]['m_PixelsToUnits']), None)
        textures = [pid for pid, o in sf.objects.items() if o.type.name == 'Texture2D']
        tmpl_tex = next((pid for pid in textures if pid not in dirty), textures[0] if textures else None)
        if tmpl_sprite is None or tmpl_tex is None:
            raise SystemExit('%s: no square local sprite/texture to model the missing covers on' % item['base'])
    remap, tex_for_sprite = {}, {}
    for key in sorted(missing):
        pc_obj = pc_shared.get(key[1])
        if pc_obj is None or pc_obj.type.name != 'Sprite':
            raise SystemExit('%s: reference %s:%d is missing on PS5 and is not a PC shared sprite' % (item['base'], missing[key], key[1]))
        spr = pc_obj.read()
        img = spr.image
        size = img.size[0]
        if img.size[0] != img.size[1]:
            raise SystemExit('%s: sprite %s is not square %s' % (item['base'], spr.m_Name, img.size))
        tex_pid, spr_pid = new_path_id(used, rng), new_path_id(used, rng)
        tt = copy.deepcopy(trees[tmpl_tex])
        tt['m_Name'] = spr.m_Name
        tt['m_IsAlphaChannelOptional'] = True
        set_texture_rgba32(tt, img)
        st = copy.deepcopy(trees[tmpl_sprite])
        st['m_Name'] = spr.m_Name
        st['m_Rect'] = {'x': 0.0, 'y': 0.0, 'width': float(size), 'height': float(size)}
        st['m_PixelsToUnits'] = float(size)
        st['m_RenderDataKey'] = [{'data[%d]' % i: rng.randrange(1 << 32) for i in range(4)}, 21300000]
        rd = st['m_RD']
        rd['texture'] = {'m_FileID': 0, 'm_PathID': tex_pid}
        rd['textureRect'] = {'x': 0.0, 'y': 0.0, 'width': float(size), 'height': float(size)}
        rd['uvTransform'] = {'x': float(size), 'y': size / 2.0, 'z': float(size), 'w': size / 2.0}
        for new_pid, tree, tmpl in ((tex_pid, tt, tmpl_tex), (spr_pid, st, tmpl_sprite)):
            clone = attr.evolve(sf.objects[tmpl], path_id=new_pid, data=None)
            clone.save_typetree(tree)
            sf.objects[new_pid] = clone
        remap[key] = spr_pid
        tex_for_sprite[spr_pid] = tex_pid
        r.setdefault('covers_added', []).append('%s %dx%d' % (spr.m_Name, size, size))

    for pid, t in trees.items():
        def rewrite(p):
            k = (p['m_FileID'], p['m_PathID'])
            return {'m_FileID': 0, 'm_PathID': remap[k]} if k in remap else None
        if walk_pptrs(t, rewrite):
            dirty.add(pid)

    ab_pid = next(pid for pid, o in sf.objects.items() if o.type.name == 'AssetBundle')
    ab = trees[ab_pid]
    old, new_table = ab['m_PreloadTable'], []
    for _, info in ab['m_Container']:
        start = len(new_table)
        for p in old[info['preloadIndex']:info['preloadIndex'] + info['preloadSize']]:
            new_table.append(p)
            if p['m_FileID'] == 0 and p['m_PathID'] in tex_for_sprite:
                new_table.append({'m_FileID': 0, 'm_PathID': tex_for_sprite[p['m_PathID']]})
        info['preloadIndex'], info['preloadSize'] = start, len(new_table) - start
    ab['m_PreloadTable'] = new_table
    dirty.add(ab_pid)

    for pid in dirty:
        sf.objects[pid].save_typetree(trees[pid])
    dst = os.path.join(out_dir, item['out_fname'])
    save_ps5(env, dst)
    r['size'] = os.path.getsize(dst)


def make_ps5_dlc_always_owned(ps5, out_dir, skip, report):
    """Set _tags 2 (DLC only) -> 3 (built-in + DLC) in every PS5 pack the catalog lists; only that object changes."""
    done = []
    for fname in pack_bundles(ps5.catalog, '/'):
        if fname in skip:
            continue
        env = UnityPy.load(os.path.join(ps5.bundles, fname))
        hit = None
        for _, sf in serialized_files(env):
            for o in sf.objects.values():
                if o.type.name != 'MonoBehaviour':
                    continue
                t = o.read_typetree()
                if '_levelProductPacks' in t and t.get('_tags') == 2:
                    t['_tags'] = TAGS_BUILTIN_AND_DLC
                    o.save_typetree(t)
                    hit = t['m_Name']
        if hit:
            save_ps5(env, os.path.join(out_dir, fname))
            done.append(fname)
            report.setdefault('ps5_dlc_always_owned', []).append(hit)
    return done


def port_songs(plan, ps5, pc, out_dir, report):
    have = {f.lower() for f in os.listdir(ps5.levels)}
    for item in plan:
        for lid in item['levels']:
            if lid.lower() in have and lid in item.get('existing', []):
                continue
            src = pc.find_song(lid)
            if src is None:
                raise SystemExit('song data for level %s (%s) not found in the PC game' % (lid, item['base']))
            env = load_as_ps5(src, ps5.unity_version)
            save_ps5(env, os.path.join(out_dir, lid.lower()))
            report.setdefault('songs', []).append('%s <- %s' % (lid.lower(), os.path.relpath(src, pc.root)))


def merge_catalog(ps5, pc, plan, changed_ps5, overlay_aa, cab_where, report):
    c = Catalog(os.path.join(ps5.aa, 'catalog.json'))
    prov_bundle = c.provider_index('UnityEngine.ResourceManagement.ResourceProviders.AssetBundleProvider')
    prov_asset = c.provider_index('UnityEngine.ResourceManagement.ResourceProviders.BundledAssetProvider')
    root = RUNTIME_ROOT + '/PS5/'
    by_id = {c.ids[e[0]]: i for i, e in enumerate(c.entries)}
    tmpl_bundle = next(i for i, e in enumerate(c.entries) if c.providers[e[1]].endswith('AssetBundleProvider') and '_pack_assets_all_' in c.ids[e[0]])
    tmpl_asset = next(i for i, e in enumerate(c.entries) if e[2] >= 0 and c.buckets[e[2]][0] == tmpl_bundle)
    shared_deps = c.buckets[c.entries[tmpl_asset][2]][1:]
    asm, cls, tmpl_opts = c.bundle_options(tmpl_bundle)
    bundle_key_has_ext = c.key(c.entries[tmpl_bundle][5]).endswith('.bundle')
    used_keys = {c.key(k) for k in range(len(c.keys_raw))}
    rc = report.setdefault('catalog', {'added': [], 'modified': []})

    def zero_crc(fname):
        e = by_id[root + fname]
        _, _, cur = c.bundle_options(e)
        size = os.path.getsize(os.path.join(overlay_aa, 'PS5', fname))
        c.set_bundle_options(e, asm, cls, dict(cur, m_Crc=0, m_BundleSize=size))
        rc['modified'].append('%s: m_Crc %s -> 0, size %s -> %d' % (fname, cur['m_Crc'], cur['m_BundleSize'], size))
        return e

    for item in plan:
        fname = item['out_fname']
        pc_assets = bundle_assets(pc.catalog, item['pc_fname'], '\\')
        if item['kind'] == 'update':
            be = zero_crc(fname)
            dep_keys = sorted({e[2] for e in c.entries if e[2] >= 0 and c.buckets[e[2]][0] == be})
            dep_key = dep_keys[0]
            dep_hash = next(c.entries[i][3] for i in range(len(c.entries)) if c.entries[i][2] == dep_key)
        else:
            size = os.path.getsize(os.path.join(overlay_aa, 'PS5', fname))
            opts = dict(tmpl_opts, m_Hash=BUNDLE_RE.match(fname).group(2), m_Crc=0, m_BundleSize=size,
                        m_BundleName=hashlib.md5(fname.encode()).hexdigest())
            be = c.add_entry([c.add_id(root + fname), prov_bundle, -1, 0, -1, 0, c.entries[tmpl_bundle][6]])
            c.set_bundle_options(be, asm, cls, opts)
            c.entries[be][5] = c.add_key(fname if bundle_key_has_ext else fname[:-len('.bundle')], [be])
            seed = fname
            while True:
                dep_str = str(seeded_rng(seed).randrange(1, 1 << 31))
                if dep_str not in used_keys:
                    break
                seed += '_'
            used_keys.add(dep_str)
            dep_key = c.add_key(dep_str, [be] + shared_deps)
            dep_hash = seeded_rng(dep_str).randrange(-(1 << 31), 1 << 31)
            rc['added'].append('%s (crc 0, size %d)' % (fname, size))
            dep_keys = [dep_key]
        for cab in item['externals']:
            if root + cab_where[cab] not in by_id:
                raise SystemExit('%s refers to %s in %s, which the PS5 catalog does not list' % (fname, cab, cab_where[cab]))
            dep = by_id[root + cab_where[cab]]
            for k in dep_keys:
                if dep not in c.buckets[k]:
                    c.buckets[k].append(dep)
                    rc['modified'].append('%s: dependency %s added' % (fname, cab_where[cab]))
        for pe in pc_assets:
            iid = pc.catalog.ids[pc.catalog.entries[pe][0]]
            keys = pc.catalog.keys_of(pe)
            if iid in by_id:
                ae = by_id[iid]
                missing = [k for k in keys if k not in c.keys_of(ae)]
                for k in missing:
                    c.add_key(k, [ae])
                if missing:
                    rc['modified'].append('%s: keys added %s' % (iid, missing))
                continue
            ae = c.add_entry([c.add_id(iid), prov_asset, dep_key, dep_hash, -1, 0, c.type_index(pc.catalog.types[pc.catalog.entries[pe][6]])])
            for k in keys:
                c.add_key(k, [ae])
            c.entries[ae][5] = c.find_key(pc.catalog.key(pc.catalog.entries[pe][5]))
            by_id[iid] = ae
            rc['added'].append('%s [%s] keys=%s' % (iid, pc.catalog.types[pc.catalog.entries[pe][6]], keys))
    for fname in changed_ps5:
        zero_crc(fname)
    c.check()
    return c


def dump_forge(cfg):
    p = cfg['dump_forge']
    p = p if os.path.isabs(p) else os.path.join(HERE, p)
    if not os.path.exists(p):
        raise SystemExit('PS5 Dump Forge not found at %s (set "dump_forge" in config.json)' % p)
    return p


def extract_ps5(pkg, work, cfg):
    dest = os.path.join(work, 'ps5_original')
    stamp = os.path.join(work, 'ps5_original.source')
    ident = '%s %d %d' % (os.path.abspath(pkg), os.path.getsize(pkg), int(os.path.getmtime(pkg)))
    if os.path.isdir(dest) and os.path.exists(stamp) and open(stamp).read() == ident:
        return dest
    if os.path.exists(dest):
        shutil.rmtree(dest)
    log('extracting the original PS5 package (one time) ...')
    subprocess.run([dump_forge(cfg), 'convert', pkg, '--to', 'folder', '-o', dest], check=True, stdout=subprocess.DEVNULL)
    open(stamp, 'w').write(ident)
    return dest


def assemble_and_pack(ps5_root, overlay, work, cfg, title):
    full = os.path.join(work, 'game')
    if os.path.exists(full):
        shutil.rmtree(full)
    if subprocess.run(['cp', '-Rc', ps5_root, full], stderr=subprocess.DEVNULL).returncode != 0:
        if os.path.exists(full):
            shutil.rmtree(full)
        shutil.copytree(ps5_root, full)
    for d, _, files in os.walk(overlay):
        for f in files:
            src = os.path.join(d, f)
            dst = os.path.join(full, os.path.relpath(src, overlay))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                os.remove(dst)
            shutil.copy2(src, dst)
    if cfg['unlock_all_dlc'] and os.path.exists(os.path.join(full, 'dlc_emu.ini')):
        os.remove(os.path.join(full, 'dlc_emu.ini'))
    pkg = os.path.join(work, title + '.pkg')
    if os.path.exists(pkg):
        os.remove(pkg)
    log('packing %s ...' % pkg)
    subprocess.run([dump_forge(cfg), 'convert', full, '--to', 'pkg', '--kraken', cfg['kraken_level'], '--full-verify', '-o', pkg],
                   check=True, stdout=subprocess.DEVNULL)
    return pkg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--ps5-pkg', help='the original PS5 package (fake-signed, plaintext)')
    src.add_argument('--ps5-dir', help='an already extracted original PS5 game folder')
    ap.add_argument('--pc-game', required=True, help='the PC game folder (holds "Beat Saber_Data" and "DLC")')
    ap.add_argument('--work', default='work', help='output and scratch folder (default ./work)')
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--no-pkg', action='store_true', help='stop after validation, before packing')
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    work = os.path.abspath(a.work)
    os.makedirs(work, exist_ok=True)
    overlay = os.path.join(work, 'overlay')
    if a.ps5_dir:
        ps5_root = os.path.realpath(a.ps5_dir)
        for owned in (overlay, os.path.join(work, 'game')):
            owned = os.path.realpath(owned)
            if ps5_root == owned or ps5_root.startswith(owned + os.sep) or owned.startswith(ps5_root + os.sep):
                raise SystemExit('--ps5-dir must not be inside, or contain, %s: the porter deletes and rebuilds it' % owned)
    else:
        ps5_root = extract_ps5(os.path.abspath(a.ps5_pkg), work, cfg)
    ps5, pc = Ps5Game(ps5_root), PcGame(os.path.abspath(a.pc_game))
    report = {'ps5_unity_version': ps5.unity_version, 'unlock_all_dlc': cfg['unlock_all_dlc'],
              'max_beatmap_version': cfg['max_beatmap_version']}

    if os.path.exists(overlay):
        shutil.rmtree(overlay)
    out_aa = os.path.join(overlay, 'Media/StreamingAssets/aa')
    out_lv = os.path.join(overlay, 'Media/StreamingAssets/BeatmapLevelsData')
    os.makedirs(os.path.join(out_aa, 'PS5'))
    os.makedirs(out_lv)

    log('comparing PC and PS5 packs ...')
    plan = discover(ps5, pc, cfg, report)
    for p in plan:
        log('  %-6s %-26s %d levels' % (p['kind'], p['base'], len(p['levels'])))
    if not plan:
        log('nothing to port: every PC pack is already in the PS5 build')

    scripts = known_scripts(os.path.join(ps5.root, 'Media/Metadata/global-metadata.dat'))
    envs = ps5_environments(ps5)
    ps5_cabs, cab_where = ps5_cab_index(ps5)
    pc_shared = {}
    for p in glob.glob(os.path.join(pc.bundles, 'sharedassets_assets_all_*.bundle')):
        for _, sf in serialized_files(UnityPy.load(p)):
            pc_shared.update(sf.objects)

    for item in plan:
        log('converting pack %s ...' % item['base'])
        fix_pack_bundle(item, ps5, pc, cfg, scripts, envs, ps5_cabs, pc_shared, os.path.join(out_aa, 'PS5'), report)
    log('converting songs ...')
    port_songs(plan, ps5, pc, out_lv, report)
    changed_ps5 = []
    if cfg['unlock_all_dlc']:
        log('making the PS5 DLC packs always owned ...')
        changed_ps5 = make_ps5_dlc_always_owned(ps5, os.path.join(out_aa, 'PS5'), {p['out_fname'] for p in plan}, report)

    log('merging the catalog ...')
    c = merge_catalog(ps5, pc, plan, changed_ps5, out_aa, cab_where, report)
    with open(os.path.join(out_aa, 'catalog.json'), 'w', encoding='utf-8') as f:
        f.write(c.serialize())
    report['converted_packs'] = [p['out_fname'] for p in plan]
    with open(os.path.join(work, 'report.json'), 'w') as f:
        json.dump(report, f, indent=1)

    log('validating ...')
    rc = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'validate.py'), ps5.root, overlay,
                         os.path.join(work, 'report.json'), os.path.join(work, 'covers.png')])
    if rc.returncode != 0:
        raise SystemExit('validation failed; see the FAIL lines above')
    if a.no_pkg:
        return
    title = '%s (v%s) Modded' % (cfg['package_title'], json.load(open(os.path.join(ps5.root, 'sce_sys/param.json')))['contentVersion'])
    pkg = assemble_and_pack(ps5.root, overlay, work, cfg, title)
    log('done: %s' % pkg)


if __name__ == '__main__':
    main()

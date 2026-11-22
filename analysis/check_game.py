#!/usr/bin/env python3
"""Re-check, in the decrypted PS5 game code, every behaviour the porter relies on.

    python analysis/check_game.py <extracted PS5 game folder> [config.json]

Run it whenever the PS5 game version changes. Each line is OK or CHANGED; a CHANGED line means the
porter's assumption must be re-verified by hand (see docs/HOW-IT-WORKS.md) before trusting a build.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

RESULTS = []
CONFIG = None


def report(ok, what, detail=''):
    RESULTS.append(ok)
    print('%-8s %s%s' % ('OK' if ok else 'CHANGED', what, ('  [%s]' % detail) if detail else ''), flush=True)


def main():
    game = os.path.abspath(sys.argv[1])
    global CONFIG
    CONFIG = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(HERE), 'config.json')
    prx = [p for p in glob.glob(os.path.join(game, 'Media/Modules/*')) if os.path.basename(p).lower() == 'il2cppuserassemblies.prx']
    if not prx:
        raise SystemExit('Media/Modules/Il2cppUserAssemblies.prx not found')
    tmp = tempfile.mkdtemp(prefix='bs-check-')
    try:
        run_checks(game, prx[0], tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%d checks, %d changed' % (len(RESULTS), RESULTS.count(False)))
    sys.exit(0 if all(RESULTS) else 1)


def run_checks(game, prx_path, tmp):
    elf = os.path.join(tmp, 'il2cpp.elf')
    subprocess.run([sys.executable, '-I', os.path.join(HERE, 'unself.py'), prx_path, elf], check=True, stdout=subprocess.DEVNULL)
    from il2 import IL
    from xref import xrefs, owner
    il = IL(elf, os.path.join(game, 'Media/Metadata/global-metadata.dat'))
    m = il.m

    def method(t, n):
        r = il.find(t, n)
        return r[0] if r else None

    def body(t, n, size=0x3000):
        mi = method(t, n)
        return il.dis(mi, size) if mi is not None and mi in il.m2addr else []

    def calls(lines, target):
        return any(target in l for l in lines)

    ti = [t for t in m.find_types('PackDefinitionSO') if m.tname(t) == 'PackDefinitionSO']
    fields = m.type_fields(ti[0]) if ti else []
    report('_tags' in fields and '_levelProductPacks' in fields, 'PackDefinitionSO has _tags and _levelProductPacks', ', '.join(fields))

    skip = ' '.join(l.split(':', 1)[1] for l in body('PackDefinitionSO', 'get_isEntitlementCheckSkipped', 0x40))
    report('and eax, 3' in skip and 'cmp eax, 2' in skip and 'setne' in skip,
           'isEntitlementCheckSkipped == ((tags & 3) != 2)', skip.strip()[:80])

    load = body('BeatmapLevelsModel', 'LoadAllBeatmapLevelPacks')
    report(calls(load, 'BeatmapLevelsEntitlementModel::AddAlwaysOwnedPack') and calls(load, 'BeatmapLevelsModel::CreateBeatmapLevelPack'),
           'LoadAllBeatmapLevelPacks registers non-DLC-only packs as always owned')

    def lit_refs(text):
        out = set()
        for i in range(len(m.lits)):
            if m.lit(i) == text:
                for g in [g for g, v in il.glob.items() if v == (5, i)]:
                    out |= {owner(il, x) for x in xrefs(il, g)}
        return {o for o in out if o}

    refs = lit_refs('PackDefinition')
    report(any('PackDefinitionAsyncInstaller' in r for r in refs), 'packs are loaded by the Addressables label "PackDefinition"', ', '.join(sorted(refs))[:120])

    refs = lit_refs('BeatmapLevelsData')
    path = body('BeatmapDataAssetsModel', 'GetAssetBundleStreamingAssetsPathForBeatmapLevelId', 0x200)
    report(any('BeatmapDataAssetsModel' in r for r in refs) and calls(path, 'System.String::ToLower') and calls(path, 'System.IO.Path::Combine'),
           'song file = StreamingAssets/BeatmapLevelsData/<levelID lower-case>')

    mi = method('UnityEngine.ResourceManagement.ResourceProviders.AssetBundleResource', 'LoadLocalBundle')
    params = None
    if mi is not None and mi in il.m2addr:
        for ins in il.e.md.disasm(il.e.rd(il.m2addr[mi], 0x200), il.m2addr[mi]):
            if ins.mnemonic in ('call', 'jmp') and ins.op_str.startswith('0x'):
                callee = il.addr2m.get(int(ins.op_str, 16))
                if callee is not None and m.mname(callee) == 'UnityEngine.AssetBundle::LoadFromFileAsync':
                    params = m.methods[callee][10]
    report(params is not None and params >= 2, 'Addressables local bundles go through LoadFromFileAsync(path, crc): changed bundles need m_Crc 0',
           'overload with %s parameters' % params)

    v4 = [m.lit(i) for i in range(len(m.lits)) if re.fullmatch(r'\d+\.\d+\.\d+', m.lit(i))]
    ctor = body('BeatmapSaveDataVersion4.BeatmapSaveData', '.ctor', 0x400)
    ver = next((re.search(r"lit '([\d.]+)'", l).group(1) for l in ctor if re.search(r"lit '([\d.]+)'", l)), None)
    cfg_max = json.load(open(CONFIG))['max_beatmap_version']
    report(ver == cfg_max, 'newest beatmap format this build writes: %s; config max_beatmap_version: %s (validate.py rejects newer songs)'
           % (ver, cfg_max), ', '.join(sorted(set(v4)))[:120])

    try:
        import UnityPy
        b = glob.glob(os.path.join(game, 'Media/StreamingAssets/aa/PS5/*_monoscripts_*.bundle'))[0]
        report(True, 'Unity version of the PS5 build: %s' % UnityPy.load(b).file.version_engine)
    except Exception as e:  # noqa: BLE001
        report(False, 'could not read the Unity version', str(e))



if __name__ == '__main__':
    main()

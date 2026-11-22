import sys, struct, bisect, pickle, os
sys.path.insert(0, os.path.dirname(__file__))
from elf import Elf
from meta import Meta
class IL:
    def __init__(self, elf_path, metadata_path, cache_path=None):
        """elf_path: Il2cppUserAssemblies.prx unwrapped by unself.py; metadata_path: Media/Metadata/global-metadata.dat."""
        self.e = Elf(elf_path); self.m = Meta(metadata_path)
        e, m = self.e, self.m
        cache = cache_path or elf_path + '.il2map.pkl'
        if os.path.exists(cache):
            self.addr2m, self.m2addr, self.glob = pickle.load(open(cache, 'rb'))
        else:
            inv = {}
            for k, v in e.rel.items(): inv.setdefault(v, []).append(k)
            self.addr2m = {}; self.m2addr = {}
            for ii in range(len(m.images)):
                nm = m.image_name(ii).encode() + b'\0'
                mod = None
                for sva in e.find_bytes(nm):
                    for slot in inv.get(sva, []):
                        mp = e.ptr(slot + 16)
                        if mp and e.v2o(mp): mod = (slot, e.u32(slot + 8), mp)
                if not mod: continue
                _, cnt, mp = mod
                im = m.images[ii]
                for ti in range(im[2], im[2] + im[3]):
                    for mi in m.type_methods(ti):
                        rid = m.methods[mi][6] & 0xFFFFFF
                        if 0 < rid <= cnt:
                            a = e.ptr(mp + 8 * (rid - 1))
                            if a:
                                self.m2addr[mi] = a; self.addr2m.setdefault(a, mi)
            self.glob = {}
            for p in e.load:
                if not p[1] & 2: continue
                base = p[3]; data = e.d[p[2]:p[2] + p[5]]
                for off in range(0, len(data) - 7, 8):
                    v = struct.unpack_from('<Q', data, off)[0]
                    if v & 1 and v < (1 << 32) and 1 <= (v >> 29) <= 6 and base + off not in e.rel:
                        self.glob[base + off] = ((v >> 29) & 7, (v & 0x1FFFFFFF) >> 1)
            pickle.dump((self.addr2m, self.m2addr, self.glob), open(cache, 'wb'))
        self.starts = sorted(self.addr2m)
    def gdesc(self, a):
        if a not in self.glob: return None
        t, i = self.glob[a]; m = self.m
        try:
            if t == 5: return 'lit %r' % m.lit(i)
            if t == 3: return 'mdef ' + m.mname(i)
            return {1: 'typeinfo', 2: 'il2cpptype', 4: 'field', 6: 'methodref'}[t] + '#%d' % i
        except Exception as ex: return 'glob(%d,%d)' % (t, i)
    def fname(self, a):
        mi = self.addr2m.get(a); return self.m.mname(mi) if mi is not None else None
    def find(self, typename, meth=None):
        out = []
        for ti in range(len(self.m.types)):
            if self.m.tname(ti) == typename or self.m.tname(ti).endswith('/' + typename):
                for mi in self.m.type_methods(ti):
                    if meth is None or self.m.s(self.m.methods[mi][0]) == meth: out.append(mi)
        return out
    def dis(self, mi, maxn=0x1200):
        a = self.m2addr[mi]; j = bisect.bisect_right(self.starts, a)
        end = self.starts[j] if j < len(self.starts) else a + maxn
        n = min(end - a, maxn); lines = []
        for ins in self.e.md.disasm(self.e.rd(a, n), a):
            s = '%x: %s %s' % (ins.address, ins.mnemonic, ins.op_str); note = ''
            if ins.mnemonic in ('call', 'jmp') and ins.op_str.startswith('0x'):
                t = int(ins.op_str, 16); note = self.fname(t) or ''
            if 'rip' in ins.op_str:
                import re
                mm = re.search(r'\[rip ([+-]) (0x[0-9a-f]+)\]', ins.op_str)
                if mm:
                    t = ins.address + ins.size + (int(mm.group(2), 16) * (1 if mm.group(1) == '+' else -1))
                    note = self.gdesc(t) or ('g_%x' % t)
            lines.append(s + ('   ; ' + note if note else ''))
        return lines

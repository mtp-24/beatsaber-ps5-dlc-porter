import struct, capstone
class Elf:
    def __init__(self, path):
        d = self.d = open(path, 'rb').read()
        phoff = struct.unpack_from('<Q', d, 0x20)[0]; n = struct.unpack_from('<H', d, 0x38)[0]
        self.ph = [struct.unpack_from('<IIQQQQQQ', d, phoff + i * 56) for i in range(n)]
        self.load = [p for p in self.ph if p[0] == 1]
        dyn = [p for p in self.ph if p[0] == 2][0]
        tags = {}
        for i in range(dyn[5] // 16):
            t, v = struct.unpack_from('<qQ', d, dyn[2] + 16 * i)
            if t == 0: break
            tags.setdefault(t, v)
        self.tags = tags
        self.rel = {}
        self.sym64 = {}
        for (a, s) in ((7, 8), (0x17, 2)):
            if a not in tags: continue
            off = self.v2o(tags[a]); size = tags[s]
            for i in range(size // 24):
                ro, info, add = struct.unpack_from('<QQq', d, off + 24 * i)
                if info & 0xffffffff == 8: self.rel[ro] = add
                else: self.sym64[ro] = (info >> 32, info & 0xffffffff, add)
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    def v2o(self, va):
        for p in self.load:
            if p[3] <= va < p[3] + p[5]: return p[2] + va - p[3]
        return None
    def rd(self, va, n):
        o = self.v2o(va); return self.d[o:o + n] if o is not None else None
    def ptr(self, va):
        if va in self.rel: return self.rel[va]
        b = self.rd(va, 8); return struct.unpack('<Q', b)[0] if b else None
    def u32(self, va): return struct.unpack('<I', self.rd(va, 4))[0]
    def find_bytes(self, b):
        out = []; i = self.d.find(b)
        while i >= 0:
            for p in self.load:
                if p[2] <= i < p[2] + p[5]: out.append(p[3] + i - p[2])
            i = self.d.find(b, i + 1)
        return out
    def dis(self, va, n=0x400):
        return list(self.md.disasm(self.rd(va, n), va))

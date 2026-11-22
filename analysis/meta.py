import struct
SECS = ['stringLiteral','stringLiteralData','string','events','properties','methods','parameterDefaultValues','fieldDefaultValues','fieldAndParameterDefaultValueData','fieldMarshaledSizes','parameters','fields','genericParameters','genericParameterConstraints','genericContainers','nestedTypes','interfaces','vtableMethods','interfaceOffsets','typeDefinitions','images','assemblies','fieldRefs','referencedAssemblies','attributeData','attributeDataRange','unresolvedIndirectCallParameterTypes','unresolvedIndirectCallParameterRanges','windowsRuntimeTypeNames','windowsRuntimeStrings','exportedTypeDefinitions']
class Meta:
    def __init__(self, path):
        d = self.d = open(path, 'rb').read()
        assert struct.unpack_from('<I', d, 0)[0] == 0xFAB11BAF
        self.version = struct.unpack_from('<i', d, 4)[0]
        self.sec = {}
        for i, n in enumerate(SECS):
            self.sec[n] = struct.unpack_from('<ii', d, 8 + 8 * i)
        self.types = self._arr('typeDefinitions', 88, '<IIiiiiiIiiiiiiiiHHHHHHHHII')
        self.methods = self._arr('methods', 36, '<IiiiiiIHHHH')
        self.images = self._arr('images', 40, '<IiiIiIiIiI')
        self.fields = self._arr('fields', 12, '<IiI')
        self.lits = self._arr('stringLiteral', 8, '<Ii')
        self.method_type = {}
        for ti, t in enumerate(self.types):
            ms, mc = t[9], t[16]
            for m in range(ms, ms + mc): self.method_type[m] = ti
        noff = self.sec['nestedTypes'][0]
        self.parent = {}
        for ti, t in enumerate(self.types):
            for k in range(t[20]):
                self.parent[struct.unpack_from('<i', self.d, noff + 4 * (t[12] + k))[0]] = ti
        self.type_image = {}
        for ii, im in enumerate(self.images):
            for t in range(im[2], im[2] + im[3]): self.type_image[t] = ii
    def _arr(self, n, sz, fmt):
        off, size = self.sec[n]; assert size % sz == 0, (n, size, sz)
        assert struct.calcsize(fmt) == sz, (n, struct.calcsize(fmt))
        return [struct.unpack_from(fmt, self.d, off + i * sz) for i in range(size // sz)]
    def s(self, idx):
        off = self.sec['string'][0] + idx; e = self.d.index(b'\0', off)
        return self.d[off:e].decode('utf-8', 'replace')
    def lit(self, i):
        ln, di = self.lits[i]; off = self.sec['stringLiteralData'][0] + di
        return self.d[off:off + ln].decode('utf-8', 'replace')
    def tname(self, ti):
        t = self.types[ti]; n = self.s(t[0]); ns = self.s(t[1])
        if ti in self.parent: return self.tname(self.parent[ti]) + '/' + n
        return (ns + '.' if ns else '') + n
    def mname(self, mi):
        return self.tname(self.method_type[mi]) + '::' + self.s(self.methods[mi][0])
    def image_name(self, ii): return self.s(self.images[ii][0])
    def find_types(self, sub):
        return [ti for ti in range(len(self.types)) if sub in self.tname(ti)]
    def type_methods(self, ti):
        t = self.types[ti]; return list(range(t[9], t[9] + t[16]))
    def type_fields(self, ti):
        t = self.types[ti]; return [self.s(self.fields[f][0]) for f in range(t[8], t[8] + t[18])]

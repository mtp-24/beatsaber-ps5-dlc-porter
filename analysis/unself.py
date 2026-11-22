import struct, sys
d = open(sys.argv[1], 'rb').read()
n = struct.unpack_from('<H', d, 0x18)[0]; eoff = 0x20 + n * 0x20
assert d[eoff:eoff+4] == b'\x7fELF', d[eoff:eoff+4]
ents = [struct.unpack_from('<QQQQ', d, 0x20 + i * 0x20) for i in range(n)]
phoff = struct.unpack_from('<Q', d, eoff + 0x20)[0]; phn = struct.unpack_from('<H', d, eoff + 0x38)[0]
phs = [struct.unpack_from('<IIQQQQQQ', d, eoff + phoff + i * 56) for i in range(phn)]
out = bytearray(max(p[2] + p[5] for p in phs))
out[:0x40 + phn * 56] = d[eoff:eoff + 0x40 + phn * 56]
for props, off, fs, ms in ents:
    if not props & 0x800: continue
    assert not props & 0xA, 'encrypted/compressed segment'
    p = phs[(props >> 20) & 0xFFF]; out[p[2]:p[2] + fs] = d[off:off + fs]
open(sys.argv[2], 'wb').write(out); print('ok', len(out), [hex(p[0]) for p in phs])

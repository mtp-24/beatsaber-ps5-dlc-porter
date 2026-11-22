import struct, lzma, sys

def lz4_block(src, usize):
    out = bytearray(); i = 0
    while i < len(src):
        t = src[i]; i += 1
        l = t >> 4
        if l == 15:
            while True:
                b = src[i]; i += 1; l += b
                if b != 255: break
        out += src[i:i+l]; i += l
        if i >= len(src): break
        off = src[i] | (src[i+1] << 8); i += 2
        m = (t & 15) + 4
        if m == 19:
            while True:
                b = src[i]; i += 1; m += b
                if b != 255: break
        s = len(out) - off
        for k in range(m): out.append(out[s+k])
    return bytes(out[:usize])

def unpack(data, comp, usize):
    c = comp & 0x3f
    if c == 0: return data[:usize]
    if c in (2, 3): return lz4_block(data, usize)
    if c == 1:
        p = data[0]; lc = p % 9; r = p // 9; lp = r % 5; pb = r // 5
        ds = struct.unpack('<I', data[1:5])[0]
        d = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{'id': lzma.FILTER_LZMA1, 'lc': lc, 'lp': lp, 'pb': pb, 'dict_size': ds}])
        return d.decompress(data[5:], max_length=usize)
    raise ValueError('comp %d' % c)

def cstr(b, p):
    e = b.index(b'\0', p); return b[p:e].decode('latin1'), e + 1

def read_bundle(path):
    b = open(path, 'rb').read()
    sig, p = cstr(b, 0)
    fmt = struct.unpack('>I', b[p:p+4])[0]; p += 4
    uver, p = cstr(b, p); urev, p = cstr(b, p)
    size, cbi, ubi, flags = struct.unpack('>qIII', b[p:p+20]); p += 20
    if fmt >= 7: p = (p + 15) & ~15
    if flags & 0x80:
        bi = b[len(b)-cbi:]; dstart = p
    else:
        bi = b[p:p+cbi]; dstart = p + cbi
    if flags & 0x200: dstart = (dstart + 15) & ~15
    bi = unpack(bi, flags, ubi)
    q = 16; nb = struct.unpack('>I', bi[q:q+4])[0]; q += 4
    blocks = []
    for _ in range(nb):
        u, c, f = struct.unpack('>IIH', bi[q:q+10]); q += 10; blocks.append((u, c, f))
    nn = struct.unpack('>I', bi[q:q+4])[0]; q += 4
    nodes = []
    for _ in range(nn):
        off, sz, fl = struct.unpack('>QQI', bi[q:q+20]); q += 20
        name, q = cstr(bi, q); nodes.append((off, sz, fl, name))
    raw = bytearray(); o = dstart
    for u, c, f in blocks:
        raw += unpack(b[o:o+c], f, u); o += c
    files = {n: bytes(raw[off:off+sz]) for off, sz, fl, n in nodes}
    return dict(sig=sig, fmt=fmt, uver=uver, urev=urev, flags=flags, blocks=blocks, nodes=nodes), files

def read_sf(d):
    msize, fsize, ver, doff = struct.unpack('>IIII', d[:16]); p = 16
    endian = d[p]; p += 4
    if ver >= 22:
        msize, fsize, doff = struct.unpack('>IQQ', d[p:p+20]); p += 20 + 8
    E = '<' if endian == 0 else '>'
    uver, p = cstr(d, p)
    plat = struct.unpack(E+'I', d[p:p+4])[0]; p += 4
    tt = d[p]; p += 1
    ntypes = struct.unpack(E+'i', d[p:p+4])[0]; p += 4
    types = []
    for _ in range(ntypes):
        cid = struct.unpack(E+'i', d[p:p+4])[0]; p += 4
        stripped = d[p]; p += 1
        sidx = struct.unpack(E+'h', d[p:p+2])[0]; p += 2
        sh = None
        if cid == 114: sh = d[p:p+16].hex(); p += 16
        p += 16
        if tt:
            nn, sl = struct.unpack(E+'ii', d[p:p+8]); p += 8
            p += nn * 32 + sl
            if ver >= 21:
                nd = struct.unpack(E+'i', d[p:p+4])[0]; p += 4 + nd * 4
        types.append((cid, sidx, sh))
    nobj = struct.unpack(E+'i', d[p:p+4])[0]; p += 4
    objs = []
    for _ in range(nobj):
        p = (p + 3) & ~3
        pid, = struct.unpack(E+'q', d[p:p+8]); p += 8
        bo, = struct.unpack(E+'Q', d[p:p+8]); p += 8
        bs, ti = struct.unpack(E+'Ii', d[p:p+8]); p += 8
        objs.append((pid, bo, bs, ti))
    nsc = struct.unpack(E+'i', d[p:p+4])[0]; p += 4
    p += nsc * 12
    p = (p + 3) & ~3 if False else p
    nex = struct.unpack(E+'i', d[p:p+4])[0]; p += 4
    ext = []
    for _ in range(nex):
        _, p = cstr(d, p); p += 16 + 4
        path, p = cstr(d, p); ext.append(path)
    return dict(ver=ver, uver=uver, plat=plat, typetree=tt, types=types, nobj=nobj, objs=objs, ext=ext, doff=doff, E=E)

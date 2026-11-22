import sys, bisect, numpy as np
sys.path.insert(0, __import__('os').path.dirname(__file__)); from il2 import IL
def xrefs(il, target):
    e = il.e; out = []
    for p in e.load:
        if not p[1] & 1: continue
        buf = np.frombuffer(e.d[p[2]:p[2] + p[5]], dtype=np.uint8)
        n = len(buf) - 4
        d32 = buf[:n].astype(np.int64) | (buf[1:n+1].astype(np.int64) << 8) | (buf[2:n+2].astype(np.int64) << 16) | (buf[3:n+3].astype(np.int64) << 24)
        d32 = np.where(d32 >= 1 << 31, d32 - (1 << 32), d32)
        va_end = p[3] + np.arange(n, dtype=np.int64) + 4
        hits = np.nonzero(va_end + d32 == target)[0]
        out += [int(p[3] + h) for h in hits]
    return out
def owner(il, va):
    j = bisect.bisect_right(il.starts, va) - 1
    return il.fname(il.starts[j]) if j >= 0 else None

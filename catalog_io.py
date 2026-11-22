"""Read and write Unity Addressables content catalogs (catalog.json, binary-in-base64 layout)."""
import base64
import json
import struct

KEY_ASCII, KEY_UNICODE, KEY_UINT16, KEY_UINT32, KEY_INT32, KEY_HASH128, KEY_TYPE, KEY_JSON = 0, 1, 2, 3, 4, 5, 6, 7


def _key_len(kd, off):
    t = kd[off]
    if t in (KEY_ASCII, KEY_UNICODE):
        return 1 + 4 + struct.unpack_from('<i', kd, off + 1)[0]
    if t == KEY_UINT16:
        return 1 + 2
    if t in (KEY_UINT32, KEY_INT32):
        return 1 + 4
    if t == KEY_HASH128:
        return 1 + 16
    raise ValueError('unsupported key type %d at %d' % (t, off))


def encode_str_key(s):
    try:
        b = s.encode('ascii')
        return bytes([KEY_ASCII]) + struct.pack('<i', len(b)) + b
    except UnicodeEncodeError:
        b = s.encode('utf-16-le')
        return bytes([KEY_UNICODE]) + struct.pack('<i', len(b)) + b


def decode_key(raw):
    t = raw[0]
    if t == KEY_ASCII:
        return raw[5:].decode('ascii')
    if t == KEY_UNICODE:
        return raw[5:].decode('utf-16-le')
    if t == KEY_UINT16:
        return struct.unpack_from('<H', raw, 1)[0]
    if t == KEY_UINT32:
        return struct.unpack_from('<I', raw, 1)[0]
    if t == KEY_INT32:
        return struct.unpack_from('<i', raw, 1)[0]
    return ('hash128', raw[1:].hex())


def encode_json_extra(assembly, cls, obj_json):
    a = assembly.encode('ascii')
    c = cls.encode('ascii')
    j = obj_json.encode('utf-16-le')
    return bytes([KEY_JSON, len(a)]) + a + bytes([len(c)]) + c + struct.pack('<i', len(j)) + j


def decode_json_extra(xd, off):
    assert xd[off] == KEY_JSON, xd[off]
    p = off + 1
    n = xd[p]; a = xd[p + 1:p + 1 + n].decode('ascii'); p += 1 + n
    n = xd[p]; c = xd[p + 1:p + 1 + n].decode('ascii'); p += 1 + n
    n = struct.unpack_from('<i', xd, p)[0]; p += 4
    return a, c, xd[p:p + n].decode('utf-16-le'), p + n - off


class Catalog:
    """Entries are lists: [internalId, provider, depKey, depHash, dataIdx, primaryKey, resourceType]."""

    def __init__(self, path):
        self.path = path
        self.raw_text = open(path, encoding='utf-8').read()
        self.j = json.loads(self.raw_text)
        j = self.j
        kd = base64.b64decode(j['m_KeyDataString'])
        bd = base64.b64decode(j['m_BucketDataString'])
        ed = base64.b64decode(j['m_EntryDataString'])
        self.xd = bytearray(base64.b64decode(j['m_ExtraDataString']))
        nkeys = struct.unpack_from('<i', kd, 0)[0]
        n = struct.unpack_from('<i', bd, 0)[0]
        assert n == nkeys
        p = 4
        self.keys_raw, self.buckets = [], []
        for _ in range(n):
            off, cnt = struct.unpack_from('<ii', bd, p); p += 8
            self.buckets.append(list(struct.unpack_from('<%di' % cnt, bd, p))); p += 4 * cnt
            self.keys_raw.append(bytes(kd[off:off + _key_len(kd, off)]))
        assert p == len(bd)
        n = struct.unpack_from('<i', ed, 0)[0]
        assert 4 + 28 * n == len(ed)
        self.entries = [list(struct.unpack_from('<7i', ed, 4 + 28 * i)) for i in range(n)]
        self._kd_orig, self._bd_orig, self._ed_orig = kd, bd, ed
        self.ids = j['m_InternalIds']
        self.providers = j['m_ProviderIds']
        self.types = [t['m_ClassName'] for t in j['m_resourceTypes']]
        self._rebuild_index()

    def _rebuild_index(self):
        self.key_index = {}
        for i, r in enumerate(self.keys_raw):
            self.key_index.setdefault(r, i)

    def key(self, i):
        return decode_key(self.keys_raw[i])

    def keys_of(self, e):
        return [self.key(k) for k, b in enumerate(self.buckets) if e in b]

    def find_key(self, s):
        return self.key_index.get(encode_str_key(s))

    def add_key(self, s, entries):
        raw = encode_str_key(s)
        if raw in self.key_index:
            k = self.key_index[raw]
            for e in entries:
                if e not in self.buckets[k]:
                    self.buckets[k].append(e)
            return k
        self.keys_raw.append(raw)
        self.buckets.append(list(entries))
        self.key_index[raw] = len(self.keys_raw) - 1
        return len(self.keys_raw) - 1

    def add_id(self, s):
        self.ids.append(s)
        return len(self.ids) - 1

    def add_extra(self, blob):
        off = len(self.xd)
        self.xd += blob
        return off

    def add_entry(self, entry):
        self.entries.append(list(entry))
        return len(self.entries) - 1

    def provider_index(self, name):
        return self.providers.index(name)

    def type_index(self, class_name):
        return self.types.index(class_name)

    def bundle_options(self, e):
        a, c, js, _ = decode_json_extra(self.xd, self.entries[e][4])
        return a, c, json.loads(js)

    def set_bundle_options(self, e, assembly, cls, opts):
        js = json.dumps(opts, separators=(',', ':'))
        self.entries[e][4] = self.add_extra(encode_json_extra(assembly, cls, js))

    def serialize(self):
        kd = bytearray(struct.pack('<i', len(self.keys_raw)))
        offs = []
        for r in self.keys_raw:
            offs.append(len(kd)); kd += r
        bd = bytearray(struct.pack('<i', len(self.buckets)))
        for off, b in zip(offs, self.buckets):
            bd += struct.pack('<ii', off, len(b)) + struct.pack('<%di' % len(b), *b)
        ed = bytearray(struct.pack('<i', len(self.entries)))
        for e in self.entries:
            ed += struct.pack('<7i', *e)
        j = dict(self.j)
        j['m_KeyDataString'] = base64.b64encode(bytes(kd)).decode('ascii')
        j['m_BucketDataString'] = base64.b64encode(bytes(bd)).decode('ascii')
        j['m_EntryDataString'] = base64.b64encode(bytes(ed)).decode('ascii')
        j['m_ExtraDataString'] = base64.b64encode(bytes(self.xd)).decode('ascii')
        j['m_InternalIds'] = self.ids
        return json.dumps(j, separators=(',', ':'), ensure_ascii=False)

    def check(self):
        nk, ne, ni = len(self.keys_raw), len(self.entries), len(self.ids)
        for i, e in enumerate(self.entries):
            iid, pr, dk, dh, di, pk, rt = e
            assert 0 <= iid < ni, (i, e)
            assert 0 <= pr < len(self.providers), (i, e)
            assert dk == -1 or 0 <= dk < nk, (i, e)
            assert di == -1 or 0 <= di < len(self.xd), (i, e)
            assert 0 <= pk < nk, (i, e)
            assert 0 <= rt < len(self.types), (i, e)
            if di >= 0:
                decode_json_extra(self.xd, di)
        for k, b in enumerate(self.buckets):
            for e in b:
                assert 0 <= e < ne, (k, e)
        return True

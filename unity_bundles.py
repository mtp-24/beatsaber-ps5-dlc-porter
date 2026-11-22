"""UnityFS bundle helpers shared by the porter and the validator."""
import random

import UnityPy
from PIL import Image

PS5_PLATFORM = 44          # BuildTarget PS5
RGBA32 = 4                 # TextureFormat.RGBA32: stored linear on PS5; block formats (BC*) are GPU-tiled
PS5_BUNDLE_LAYOUT = (0x243, 3)  # every stock PS5 bundle: LZ4HC blocks, block info at the start, padding

UnityPy.config.FALLBACK_UNITY_VERSION = '2022.3.33f1'


def serialized_files(env):
    out = []
    for f in env.files.values():
        for sub, sf in getattr(f, 'files', {}).items():
            if hasattr(sf, 'objects'):
                out.append((sub, sf))
    return out


def load_as_ps5(path, ps5_unity_version):
    """Load a bundle and retarget it to the PS5 player: engine version strings and target platform.

    Object data is left as it is; the PS5 runtime reads it through the type trees the bundles carry.
    """
    env = UnityPy.load(path)
    env.file.version_engine = ps5_unity_version
    for _, sf in serialized_files(env):
        sf.unity_version = ps5_unity_version
        sf._m_target_platform = PS5_PLATFORM
        sf.mark_changed()
    return env


def save_ps5(env, path):
    with open(path, 'wb') as f:
        f.write(env.file.save(packer=PS5_BUNDLE_LAYOUT))


def is_pptr(d):
    return isinstance(d, dict) and set(d.keys()) == {'m_FileID', 'm_PathID'}


def walk_pptrs(node, fn):
    """Call fn(pptr) on every PPtr in a type tree; fn returns a replacement dict or None. True if anything changed."""
    changed = False
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for k, v in list(items):
        if is_pptr(v):
            r = fn(v)
            if r is not None:
                node[k] = r
                changed = True
        elif isinstance(v, (dict, list)):
            changed |= walk_pptrs(v, fn)
    return changed


def iter_pptrs(node):
    stack = [node]
    while stack:
        x = stack.pop()
        if is_pptr(x):
            yield x
        elif isinstance(x, dict):
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)


def new_path_id(used, rng):
    while True:
        pid = rng.randrange(-(1 << 63), 1 << 63)
        if pid and pid not in used:
            used.add(pid)
            return pid


def rgba32_mips(img):
    """Linear RGBA32 mip chain in Unity's layout: largest first, rows bottom-up."""
    img = img.convert('RGBA')
    w, h = img.size
    data, mips = bytearray(), 0
    while True:
        data += img.resize((w, h), Image.LANCZOS).transpose(Image.FLIP_TOP_BOTTOM).tobytes()
        mips += 1
        if w == 1 and h == 1:
            break
        w, h = max(1, w // 2), max(1, h // 2)
    return bytes(data), mips


def rgba32_size(w, h, mips):
    n = 0
    for _ in range(mips):
        n += w * h * 4
        w, h = max(1, w // 2), max(1, h // 2)
    return n


def set_texture_rgba32(tree, img):
    """Rewrite a Texture2D type tree in place as linear RGBA32 with mips, data inline."""
    data, mips = rgba32_mips(img)
    tree['m_Width'], tree['m_Height'] = img.size
    tree['m_TextureFormat'] = RGBA32
    tree['m_MipCount'] = mips
    tree['m_CompleteImageSize'] = len(data)
    tree['m_IsReadable'] = False
    tree['m_IsPreProcessed'] = False
    tree['m_StreamData'] = {'offset': 0, 'size': 0, 'path': ''}
    tree['image data'] = data


def seeded_rng(*parts):
    return random.Random('|'.join(str(p) for p in parts))

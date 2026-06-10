import numpy as np
from PIL import Image

# ── wHash ─────────────────────────────────────────────────────────────────────
def _haar_ll(arr):
    lo = (arr[:, 0::2] + arr[:, 1::2]) / 2
    return (lo[0::2, :] + lo[1::2, :]) / 2

def whash(img: Image.Image, hash_size: int = 8) -> int:
    target = max(hash_size, 2) * 8
    arr = np.array(img.resize((target, target), Image.LANCZOS), dtype=np.float32)
    for _ in range(3):
        arr = _haar_ll(arr)
    flat = arr.flatten()
    bits = flat > np.mean(flat)
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v

# ── dHash ─────────────────────────────────────────────────────────────────────
def dhash(img: Image.Image, hash_size: int = 8) -> int:
    arr = np.array(img.resize((hash_size+1, hash_size), Image.LANCZOS),
                   dtype=np.int32)
    diff = arr[:, 1:] > arr[:, :-1]
    v = 0
    for b in diff.flatten():
        v = (v << 1) | int(b)
    return v

# ── Ring hash — INNER REGION ONLY ────────────────────────────────────────────
# FIX: use only inner 60% radius of image.
# Camera photos: document content occupies the centre, background is edge.
# By computing ring means only within radius < 0.6 * max_r, we avoid
# background pixels entirely → hash is stable regardless of what's behind.
_RING_SIZE   = 64
_N_RINGS     = 16
_INNER_RATIO = 0.60   # only use inner 60% of radius

def ring_hash(img: Image.Image) -> int:
    SIZE = _RING_SIZE
    gray = img.convert('L').resize((SIZE, SIZE), Image.LANCZOS)
    arr  = np.array(gray, dtype=np.float32)
    cy = cx = (SIZE - 1) / 2.0
    ys = np.arange(SIZE)[:, None].astype(np.float32)
    xs = np.arange(SIZE)[None, :].astype(np.float32)
    dist = np.sqrt((ys - cy)**2 + (xs - cx)**2)
    max_r = dist.max() * _INNER_RATIO   # ← only inner 60%

    means = []
    for ring in range(_N_RINGS):
        lo = ring     / _N_RINGS * max_r
        hi = (ring+1) / _N_RINGS * max_r
        mask = (dist >= lo) & (dist < hi)
        means.append(float(arr[mask].mean()) if mask.any() else 128.0)

    arr_m  = np.array(means)
    thresh = arr_m.mean()
    v = 0
    for m in arr_m:
        v = (v << 1) | (1 if m > thresh else 0)
    return v

def ring_hamming(a: int, b: int) -> int:
    return bin(a ^ b).count('1')

# ── Centre hash — for camera photos ──────────────────────────────────────────
# Crops the central 40%×40% of the image and computes a small wHash on it.
# For a diamond-shaped camera photo, the document centre is ALWAYS present
# regardless of rotation or background. Two photos of the same page will
# have identical/similar centres → similar centre_hash.
# This is used as an additional bucket key in nodes.py.
_CENTRE_CROP = 0.40   # use central 40% width/height

def centre_hash(img: Image.Image, hash_size: int = 8) -> int:
    W, H = img.size
    cx, cy = W // 2, H // 2
    cw, ch = int(W * _CENTRE_CROP / 2), int(H * _CENTRE_CROP / 2)
    crop = img.crop((cx-cw, cy-ch, cx+cw, cy+ch))
    return whash(crop, hash_size)

# ── Helpers ───────────────────────────────────────────────────────────────────
def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count('1')

def hash_page(page: dict) -> dict:
    img        = page['image']
    page['wh'] = whash(img)
    page['dh'] = dhash(img)
    page['rh'] = ring_hash(img)     # inner-60% ring hash
    page['ch'] = centre_hash(img)   # central-crop hash
    return page

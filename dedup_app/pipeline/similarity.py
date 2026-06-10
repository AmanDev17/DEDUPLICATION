import numpy as np
from PIL import Image
from .hashing import hamming, ring_hamming

RING_MAX      = 6      # ring Hamming threshold
CENTRE_MAX    = 14     # centre_hash Hamming threshold (looser — broader buckets)
WHASH_MAX     = 26     # wHash Hamming (loose, only kills clearly different)
SSIM_CONFIRM  = 0.85   # windowed SSIM → confirmed duplicate
AKAZE_RATIO   = 0.22   # keypoint good_matches / min(kp_a, kp_b)

PATCH_SIZE    = 64
PATCH_STRIDE  = 32

# Angle sweeps
ORTHO_ANGLES  = [90, 180, 270]
COARSE_ANGLES = list(range(-45, 46, 5))
FINE_WINDOW   = 5
# Extended sweep for camera photos (any angle possible)
EXTENDED_ANGLES = list(range(-90, 91, 10))


# ── Inscribed crop (removes fill corners after rotation) ──────────────────────
def _inscribed_crop(img: Image.Image, angle_deg: float) -> Image.Image:
    import math
    W, H  = img.size
    a     = math.radians(abs(angle_deg % 90))
    if a < 0.01:
        return img
    cos_a, sin_a = math.cos(a), math.sin(a)
    if W >= H:
        inner_w = max(10, min(W, int(H*cos_a - W*sin_a + W*cos_a - H*sin_a)))
        inner_h = max(10, min(H, int(H*cos_a - W*sin_a)))
    else:
        inner_w = max(10, min(W, int(W*cos_a - H*sin_a)))
        inner_h = max(10, min(H, int(W*cos_a - H*sin_a + H*cos_a - W*sin_a)))
    cx, cy = W//2, H//2
    cropped = img.crop((cx-inner_w//2, cy-inner_h//2,
                        cx+inner_w//2, cy+inner_h//2))
    return cropped.resize((W, H), Image.LANCZOS)


# ── Centre crop helper ────────────────────────────────────────────────────────
def _centre_crop(img: Image.Image, ratio: float = 0.55) -> Image.Image:
    """Crop central ratio×ratio region. Ignores background/border."""
    W, H = img.size
    cw, ch = int(W * ratio / 2), int(H * ratio / 2)
    cx, cy = W // 2, H // 2
    return img.crop((cx-cw, cy-ch, cx+cw, cy+ch)).resize((W, H), Image.LANCZOS)


# ── Windowed SSIM ─────────────────────────────────────────────────────────────
def _ssim_patch(a: np.ndarray, b: np.ndarray) -> float:
    C1 = (0.01*255)**2; C2 = (0.03*255)**2
    ma, mb = a.mean(), b.mean()
    cov = ((a-ma)*(b-mb)).mean()
    num = (2*ma*mb+C1) * (2*cov+C2)
    den = (ma**2+mb**2+C1) * (a.var()+b.var()+C2)
    return float(num/den) if den else 1.0

def windowed_ssim(img_a: Image.Image, img_b: Image.Image) -> float:
    a = np.array(img_a, dtype=np.float32)
    b = np.array(img_b, dtype=np.float32)
    H, W = a.shape
    scores = []
    for y in range(0, H-PATCH_SIZE+1, PATCH_STRIDE):
        for x in range(0, W-PATCH_SIZE+1, PATCH_STRIDE):
            pa = a[y:y+PATCH_SIZE, x:x+PATCH_SIZE]
            pb = b[y:y+PATCH_SIZE, x:x+PATCH_SIZE]
            if pa.std() < 5.0 and pb.std() < 5.0:
                continue
            scores.append(_ssim_patch(pa, pb))
    return float(np.mean(scores)) if scores else _ssim_patch(a, b)

def _rotate_crop(img: Image.Image, angle: float) -> Image.Image:
    if angle == 0:
        return img
    rot = img.rotate(angle, expand=False, fillcolor=255, resample=Image.BICUBIC)
    return _inscribed_crop(rot, angle)


# ── Tilt-robust SSIM with centre-crop mode ────────────────────────────────────
def ssim_tilt_robust(img_a: Image.Image, img_b: Image.Image,
                     is_photo_pair: bool = False) -> tuple:
    """
    is_photo_pair=True: use centre crop + extended angle sweep
    is_photo_pair=False: full image + standard sweep
    """
    if is_photo_pair:
        # For camera photos: compute SSIM on centre crop only
        # This ignores the background that varies between photos
        a_use = _centre_crop(img_a)
        b_use = _centre_crop(img_b)
        sweep = EXTENDED_ANGLES   # ±90° in 10° steps
    else:
        a_use = img_a
        b_use = img_b
        sweep = COARSE_ANGLES

    best_score = windowed_ssim(a_use, b_use)
    best_angle = 0.0

    # Orthogonal fast path
    for angle in ORTHO_ANGLES:
        s = windowed_ssim(a_use, _rotate_crop(b_use, angle))
        if s > best_score:
            best_score, best_angle = s, float(angle)
        if best_score >= SSIM_CONFIRM:
            return best_score, best_angle

    # Coarse/extended sweep
    for angle in sweep:
        s = windowed_ssim(a_use, _rotate_crop(b_use, angle))
        if s > best_score:
            best_score, best_angle = s, float(angle)
        if best_score >= SSIM_CONFIRM:
            return best_score, best_angle

    # Fine sweep around best (only if we got a promising lead ≥ 0.65)
    if best_score >= 0.65:
        centre = int(best_angle)
        for angle in range(centre - FINE_WINDOW, centre + FINE_WINDOW + 1):
            if angle % 5 == 0 or angle % 90 == 0:
                continue
            s = windowed_ssim(a_use, _rotate_crop(b_use, angle))
            if s > best_score:
                best_score, best_angle = s, float(angle)
            if best_score >= SSIM_CONFIRM:
                break

    return best_score, best_angle


# ── AKAZE ─────────────────────────────────────────────────────────────────────
def _akaze_ratio(img_a: Image.Image, img_b: Image.Image,
                 is_photo_pair: bool = False) -> float:
    try:
        import cv2
        # For photo pairs: use centre crop to avoid background keypoints
        if is_photo_pair:
            img_a = _centre_crop(img_a, ratio=0.65)
            img_b = _centre_crop(img_b, ratio=0.65)
        a8 = np.array(img_a, dtype=np.uint8)
        b8 = np.array(img_b, dtype=np.uint8)
        ak = cv2.AKAZE_create(
            descriptor_type=cv2.AKAZE_DESCRIPTOR_MLDB,
            threshold=0.0003,   # lower threshold → more keypoints → better for photos
            nOctaves=4, nOctaveLayers=4)
        kp_a, des_a = ak.detectAndCompute(a8, None)
        kp_b, des_b = ak.detectAndCompute(b8, None)
        if des_a is None or des_b is None or len(kp_a)<6 or len(kp_b)<6:
            return 0.0
        bf   = cv2.BFMatcher(cv2.NORM_HAMMING)
        raw  = bf.knnMatch(des_a, des_b, k=2)
        good = [m for m,n in raw
                if len((m,n))==2 and m.distance < 0.75*n.distance]
        return len(good) / min(len(kp_a), len(kp_b))
    except Exception:
        return 0.0


# ── Result ────────────────────────────────────────────────────────────────────
class SimilarityResult:
    __slots__ = ('is_duplicate','score','stage','angle','reject_reason')
    def __init__(self, is_dup, score, stage, angle=0.0, reject_reason=''):
        self.is_duplicate  = is_dup
        self.score         = score
        self.stage         = stage
        self.angle         = angle
        self.reject_reason = reject_reason   # for unique/ folder labelling

    def __repr__(self):
        return (f'SimilarityResult(dup={self.is_duplicate},'
                f'score={self.score:.3f},stage={self.stage},'
                f'angle={self.angle}°,reason={self.reject_reason!r})')


# ── Main compare ──────────────────────────────────────────────────────────────
def compare(page_a: dict, page_b: dict) -> SimilarityResult:
    """
    Gate cascade with camera-photo awareness.

    Gate 1: EITHER ring_hash OR centre_hash must be similar.
            If both are too different → truly different content.

    Gate 2: Loose wHash gate (only kills completely different pages).

    Gate 3: SSIM — photo pair uses centre crop + extended sweep.
            Scanner pair uses full image + standard sweep.

    Gate 4: AKAZE — always runs if SSIM < SSIM_CONFIRM.
            Photo pair uses centre crop for AKAZE too.
    """
    r_dist = ring_hamming(page_a['rh'], page_b['rh'])
    c_dist = hamming(page_a['ch'], page_b['ch'])

    # ── Gate 1: ring OR centre must pass ─────────────────────────────────
    ring_ok   = r_dist <= RING_MAX
    centre_ok = c_dist <= CENTRE_MAX

    if not ring_ok and not centre_ok:
        reason = (f"ring_hamming={r_dist}>{RING_MAX} AND "
                  f"centre_hamming={c_dist}>{CENTRE_MAX}")
        return SimilarityResult(False, 0.0, 'gate1_reject', reject_reason=reason)

    # Is this a camera photo pair? (ring failed but centre passed)
    is_photo_pair = (not ring_ok and centre_ok)

    # ── Gate 2: wHash Hamming ─────────────────────────────────────────────
    wh_dist = hamming(page_a['wh'], page_b['wh'])
    if wh_dist > WHASH_MAX and not centre_ok:
        reason = f"wHash_hamming={wh_dist}>{WHASH_MAX}"
        return SimilarityResult(False, 0.0, 'gate2_reject', reject_reason=reason)

    # ── Gate 3: SSIM (with centre-crop mode for photo pairs) ─────────────
    score, angle = ssim_tilt_robust(
        page_a['image'], page_b['image'], is_photo_pair=is_photo_pair)

    if score >= SSIM_CONFIRM:
        stage = 'ssim_photo' if is_photo_pair else 'ssim'
        return SimilarityResult(True, score, stage, angle)

    # ── Gate 4: AKAZE — no reject cutoff ─────────────────────────────────
    ratio = _akaze_ratio(page_a['image'], page_b['image'],
                         is_photo_pair=is_photo_pair)
    if ratio >= AKAZE_RATIO:
        stage = 'akaze_photo' if is_photo_pair else 'akaze'
        return SimilarityResult(True, ratio, stage, angle)

    reason = (f"SSIM={score:.3f}<{SSIM_CONFIRM}, "
              f"AKAZE_ratio={ratio:.3f}<{AKAZE_RATIO}")
    return SimilarityResult(False, max(score, ratio), 'reject',
                            angle, reject_reason=reason)

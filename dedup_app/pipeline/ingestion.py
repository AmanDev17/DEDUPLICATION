import io, math
from pathlib import Path
from PIL import Image, ImageFilter, ImageOps
import numpy as np

SUPPORTED_EXTENSIONS = {
    '.pdf','.docx','.doc','.png','.jpg','.jpeg',
    '.tiff','.tif','.bmp','.webp'
}
TARGET_SIZE   = 512
DISPLAY_SIZE  = 800
JPEG_QUALITY  = 82


# ── Document region extraction ─────────────────────────────────────────────────

def extract_document_region(img: Image.Image) -> tuple:
    W, H  = img.size
    gray  = img.convert('L')
    arr   = np.array(gray, dtype=np.float32)

    # Sample border ring
    border = np.concatenate([
        arr[:20, :].flatten(),
        arr[-20:, :].flatten(),
        arr[:, :20].flatten(),
        arr[:, -20:].flatten(),
    ])
    bg_mean = float(np.median(border))
    bg_std  = float(np.std(border))

    # Uniform border + not white (white = scanner page margin)
    is_camera = bg_std < 18.0 and bg_mean < 235.0

    if not is_camera:
        return img, False

    # Find document pixels
    diff     = np.abs(arr - bg_mean)
    threshold = max(20.0, bg_std * 3.0)
    doc_mask = diff > threshold

    # Dilate to fill gaps
    mask_img = Image.fromarray((doc_mask * 255).astype(np.uint8))
    mask_img = mask_img.filter(ImageFilter.MaxFilter(size=11))
    doc_mask = np.array(mask_img) > 128

    rows = np.where(doc_mask.any(axis=1))[0]
    cols = np.where(doc_mask.any(axis=0))[0]

    if len(rows) < 30 or len(cols) < 30:
        return img, False

    y1, y2 = int(rows[0]), int(rows[-1])
    x1, x2 = int(cols[0]), int(cols[-1])

    # Padding
    pad = 12
    y1 = max(0, y1 - pad); y2 = min(H, y2 + pad)
    x1 = max(0, x1 - pad); x2 = min(W, x2 + pad)

    coverage = (x2 - x1) * (y2 - y1) / (W * H)

    # Only extract if document is meaningfully smaller than frame
    if coverage > 0.80:
        return img, False

    cropped   = img.crop((x1, y1, x2, y2))
    extracted = cropped.resize((W, H), Image.LANCZOS)
    return extracted, True


# ── Deskew ────────────────────────────────────────────────────────────────────

def _deskew(gray_img: Image.Image) -> tuple:
    """Two-pass deskew. Returns (deskewed, angle). Fine pass only if needed."""
    arr    = np.array(gray_img, dtype=np.uint8)
    binary = (arr < 128).astype(np.float32)
    if binary.sum() < 100:
        return gray_img, 0.0

    best_angle = 0.0
    best_score = float(np.var(binary.sum(axis=1)))

    for angle in range(-45, 46, 5):
        if angle == 0: continue
        rot   = gray_img.rotate(angle, expand=False, fillcolor=255)
        score = float(np.var((np.array(rot) < 128).astype(np.float32).sum(axis=1)))
        if score > best_score:
            best_score, best_angle = score, float(angle)

    if abs(best_angle) < 0.5:
        return gray_img, 0.0

    # Fine pass around best coarse angle
    coarse = best_angle
    for angle in range(int(coarse) - 4, int(coarse) + 5, 1):
        if angle % 5 == 0: continue
        rot   = gray_img.rotate(angle, expand=False, fillcolor=255)
        score = float(np.var((np.array(rot) < 128).astype(np.float32).sum(axis=1)))
        if score > best_score:
            best_score, best_angle = score, float(angle)

    if abs(best_angle) < 0.5:
        return gray_img, 0.0

    return gray_img.rotate(best_angle, expand=False, fillcolor=255), best_angle


# ── Color preservation ────────────────────────────────────────────────────────

def _encode_color(color_img: Image.Image, deskew_angle: float) -> bytes:
    img = color_img.convert('RGB')
    if abs(deskew_angle) > 0.5:
        img = img.rotate(deskew_angle, expand=False,
                         fillcolor=(255, 255, 255), resample=Image.BICUBIC)
    w, h  = img.size
    scale = DISPLAY_SIZE / max(w, h)
    if scale < 1.0:
        img = img.resize((int(w*scale), int(h*scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def _normalise_gray(img: Image.Image) -> tuple:
    """
    Full normalisation: extract document region → grayscale →
    autocontrast → deskew → resize → denoise.
    Returns (gray_512, deskew_angle, was_extracted).
    """
    # Extract document if camera photo
    img, was_extracted = extract_document_region(img)

    gray  = img.convert('L')
    gray  = ImageOps.autocontrast(gray, cutoff=2)
    gray, angle = _deskew(gray)
    gray  = gray.resize((TARGET_SIZE, TARGET_SIZE), Image.LANCZOS)
    gray  = gray.filter(ImageFilter.MedianFilter(size=3))
    return gray, angle, was_extracted


# ── File type readers ──────────────────────────────────────────────────────────

def _ingest_pdf(path: Path):
    import fitz
    doc = fitz.open(str(path))
    for page_no in range(len(doc)):
        page = doc[page_no]
        mat  = fitz.Matrix(2.0, 2.0)
        pix  = page.get_pixmap(matrix=mat, alpha=False)
        color_img = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        gray, angle, extracted = _normalise_gray(color_img)
        # For color thumb: use extracted version for display too
        display_img = color_img
        if extracted:
            display_img, _ = extract_document_region(color_img)
        color_bytes = _encode_color(display_img, angle)
        yield page_no, gray, color_bytes, extracted
    doc.close()


def _ingest_docx(path: Path, tmp_dir: Path):
    import subprocess, shutil
    lo = shutil.which('libreoffice') or shutil.which('soffice')
    if lo:
        subprocess.run([lo,'--headless','--convert-to','pdf',
                        '--outdir',str(tmp_dir),str(path)],
                       capture_output=True, timeout=120)
        pdf_path = tmp_dir / (path.stem + '.pdf')
        if pdf_path.exists():
            yield from _ingest_pdf(pdf_path)
            pdf_path.unlink(missing_ok=True)
            return
    from docx import Document as DocxDocument
    doc = DocxDocument(str(path))
    idx = 0
    for rel in doc.part.rels.values():
        if 'image' in rel.reltype:
            try:
                color_img = Image.open(io.BytesIO(rel.target_part.blob))
                gray, angle, extracted = _normalise_gray(color_img)
                color_bytes = _encode_color(color_img, angle)
                yield idx, gray, color_bytes, extracted
                idx += 1
            except Exception:
                continue
    if idx == 0:
        blank = Image.new('RGB', (TARGET_SIZE, TARGET_SIZE), (255,255,255))
        buf   = io.BytesIO()
        blank.save(buf, 'JPEG', quality=70)
        yield 0, blank.convert('L').resize((TARGET_SIZE,TARGET_SIZE)), buf.getvalue(), False


def _ingest_image(path: Path):
    color_img = Image.open(str(path))
    gray, angle, extracted = _normalise_gray(color_img)
    display_img = color_img
    if extracted:
        display_img, _ = extract_document_region(color_img)
    color_bytes = _encode_color(display_img, angle)
    yield 0, gray, color_bytes, extracted


# ── Public API ─────────────────────────────────────────────────────────────────

def ingest_file(file_path, source_label: str, tmp_dir: Path):
    """
    Yields page dicts:
      {
        'image':       PIL.Image grayscale 512×512  — for hashing+SSIM
        'color_bytes': bytes JPEG color image       — for thumbnails
        'source':      str
        'page_index':  int
        'extracted':   bool  — True if document was extracted from background
      }
    """
    path = Path(file_path)
    ext  = path.suffix.lower()

    if ext == '.pdf':
        gen = _ingest_pdf(path)
    elif ext in ('.docx', '.doc'):
        gen = _ingest_docx(path, tmp_dir)
    elif ext in ('.png','.jpg','.jpeg','.tiff','.tif','.bmp','.webp'):
        gen = _ingest_image(path)
    else:
        return

    for page_idx, gray_img, color_bytes, extracted in gen:
        yield {
            'image':       gray_img,
            'color_bytes': color_bytes,
            'source':      source_label,
            'page_index':  page_idx,
            'extracted':   extracted,
        }

# Document Deduplication Pipeline — README

## What the System Does

This is a **multi-format document deduplication pipeline** built as a Django web application. It accepts uploads of PDFs, Word documents (`.docx`/`.doc`), and image files, then detects duplicate pages — whether they appear across different folders, within the same folder, or within a single document. Results are surfaced through a structured JSON report with thumbnail previews of every cluster of duplicates and every unique page.

The pipeline is designed to handle both **scanned/scanner-produced documents** (clean white backgrounds, aligned text) and **camera-captured photos of documents** (variable backgrounds, possible tilt, arbitrary rotation). It adapts its detection strategy based on which kind of content it is comparing.

---

## Directory Structure

```
dedup_project_v7/
├── dedup_app/                     # Django application root
│   ├── pipeline/                  # Core detection logic (pure Python, no Django)
│   │   ├── __init__.py
│   │   ├── ingestion.py           # File reading and image normalisation
│   │   ├── hashing.py             # Perceptual hash functions (wHash, dHash, ring hash, centre hash)
│   │   ├── nodes.py               # Candidate pair generation via hash buckets
│   │   ├── similarity.py          # Gate cascade: SSIM + AKAZE comparison
│   │   ├── graph.py               # Sparse graph + Union-Find clustering
│   │   ├── reporter.py            # JSON report + thumbnail writer
│   │   └── run_pipeline.py        # Orchestrator — ties all stages together
│   ├── templates/                 # Django HTML templates (UI)
│   ├── static/                    # CSS, JS, assets
│   ├── __init__.py
│   ├── apps.py
│   ├── urls.py
│   └── views.py                   # Django views (file upload, job status, report display)
├── dedup_project/                 # Django project settings
├── media/
│   ├── uploads/                   # Uploaded files land here
│   ├── tmp/                       # Temporary PDF conversions from .docx
│   └── clusters/                  # Output: per-job result directories
├── myenv/                         # Python virtual environment
├── manage.py
├── .gitignore
└── README.md
```

---

## Flow of Execution

### Stage 1 — Ingestion (`ingestion.py`)

Every uploaded file is read and converted to a **normalised grayscale 512×512 PIL image** plus a **JPEG colour thumbnail** (up to 800 px on the long edge) for display purposes.

**Per file type:**
- `.pdf` — rendered at 2× scale via PyMuPDF (`fitz`), one dict per page.
- `.docx` / `.doc` — converted to PDF via LibreOffice if available; otherwise the embedded images are extracted directly using `python-docx`.
- Images (`.png`, `.jpg`, `.tiff`, `.bmp`, `.webp`) — opened directly with Pillow.

**Normalisation pipeline applied to every page image:**

1. **Document region extraction** — detects whether the image is a camera photo (non-white, low-variance border) using a border-ring pixel sample. If so, it finds the document bounding box via pixel-difference thresholding and a dilation pass, then crops and rescales to fill the frame. Pages where the document already fills > 80 % of the frame are left unchanged.

2. **Autocontrast** — `ImageOps.autocontrast` with a 2 % cutoff stretches the histogram for consistent grayscale range.

3. **Deskew** — a two-pass rotation search: coarse pass at 5° steps from −45° to +45°, then a fine 1°-step pass around the best coarse angle. Angle is selected by maximising the variance of row-sum projections (standard Hough-free deskew criterion). Skips correction if best angle < 0.5°.

4. **Resize to 512×512** and **median filter** (kernel 3) to suppress salt-and-pepper noise.

Each page becomes a dict:
```python
{
    'image':       PIL.Image,   # grayscale 512×512 — used for hashing and SSIM
    'color_bytes': bytes,       # JPEG thumbnail — used for the HTML report
    'source':      str,         # upload label (folder/filename)
    'page_index':  int,
    'extracted':   bool,        # True if document was cropped from background
}
```

---

### Stage 2 — Hashing (`hashing.py`)

Four complementary perceptual hashes are computed for every page. They are designed to complement one another: each is strong in cases where another is weak.

| Hash | Purpose | Bit width | Notes |
|------|---------|-----------|-------|
| **wHash** (wavelet hash) | Global structure | 64 bits | Three-level Haar LL sub-band, threshold by mean. Captures layout and low-frequency content. |
| **dHash** (difference hash) | Horizontal gradients | 64 bits | 9×8 resize, pixel-pair differences. Fast and shift-tolerant. |
| **ring_hash** | Rotation-invariant rings | 16 bits | Mean pixel intensity in 16 concentric rings, using only the **inner 60 % of the radius**. The inner-only constraint means camera-photo backgrounds — which sit at the outer edge — do not pollute the hash. Threshold by ring-mean average. |
| **centre_hash** | Centre-crop wHash | 64 bits | Central 40 %×40 % crop, then wHash. For a diamond-shaped rotated photo the document centre is always visible regardless of rotation angle or background. Used as an additional bucket key for camera-photo pairs. |

**Why these ranges?**

- `ring_hash` uses 16 rings with 16 bits because more rings would over-fit to noise and fewer would lose discrimination power. Inner 60 % eliminates ~36 % of the pixel area that in camera photos is typically background — keeping the hash content-focused.
- `centre_hash` crops 40 % of width/height (16 % of area) — small enough to be robust to rotation at any angle, large enough to encode meaningful content.

---

### Stage 3 — Candidate Pair Generation (`nodes.py`)

An exhaustive O(n²) comparison of all page pairs is too slow for large collections. Instead, pages are placed into **hash buckets** and only pages sharing a bucket are compared.

Four bucket families are built:

| Prefix | Hash used | Top bits | Rationale |
|--------|-----------|----------|-----------|
| `W…` | wHash | 8 bits | 256 buckets — narrow, catches aligned scanner pairs |
| `D…` | dHash | 8 bits | 256 buckets — complementary edge-gradient structure |
| `R…` | ring_hash | 6 bits | 64 buckets — broader, catches rotated/tilted pairs |
| `C…` | centre_hash | 6 bits | 64 buckets — catches camera photos of the same page |

**Why 8 bits for wHash/dHash, 6 bits for ring/centre?**
- wHash and dHash are 64-bit hashes over the full image — 8 top bits gives tight buckets with low false-positive rates for well-aligned images.
- ring_hash is only 16 bits wide and centre_hash is noisier (small crop). Using 6 bits keeps recall high — pairs that differ due to perspective or tilt still land in the same bucket.

If any bucket exceeds 500 members (could cause O(n²) blowup), it is **sub-divided** using bits 48–55 of the original hash value, capped at 500 per sub-bucket.

The output is a deduplicated `Set[Tuple[int, int]]` of page-index pairs to evaluate.

---

### Stage 4 — Similarity Comparison (`similarity.py`)

Each candidate pair passes through a **gate cascade**. Gates are ordered cheapest → most expensive. The pipeline short-circuits as soon as a pair is confirmed as a duplicate or conclusively rejected.

**Camera-photo detection:** if `ring_hash` Hamming distance > `RING_MAX` but `centre_hash` Hamming distance ≤ `CENTRE_MAX`, the pair is classified as a **photo pair** (`is_photo_pair = True`). This switches both SSIM and AKAZE to centre-crop mode.

#### Gate 1 — Ring OR Centre hash (cheap, integer arithmetic)

```
ring_hamming(a.rh, b.rh) ≤ 6   OR   hamming(a.ch, b.ch) ≤ 14
```

- `RING_MAX = 6` — allows up to 6 of 16 ring bits to differ. This tolerates moderate print/scan variation while rejecting truly different content.
- `CENTRE_MAX = 14` — looser because the centre crop is noisier; 14 of 64 bits can differ.
- If **both** fail → `gate1_reject`. The content profiles are fundamentally different.

#### Gate 2 — wHash Hamming (cheap)

```
hamming(a.wh, b.wh) ≤ 26   OR   centre passed (photo pair)
```

- `WHASH_MAX = 26` — a permissive threshold. This gate exists only to kill clearly different pages that shared a bucket by coincidence. Photo pairs bypass this gate because the full-image wHash is unstable across different camera angles.

#### Gate 3 — Windowed SSIM (medium cost)

SSIM is computed over non-uniform 64×64 patches (stride 32) to avoid being dominated by blank margins. Patches with standard deviation < 5 on both sides are skipped (blank regions carry no information).

A **tilt-robust** sweep is applied:
- **Scanner pairs:** orthogonal angles (90°/180°/270°) first, then coarse ±45° at 5° steps, then fine ±5° around the best coarse angle if SSIM ≥ 0.65.
- **Photo pairs:** extended ±90° sweep at 10° steps (any rotation is possible), applied on the **centre crop** only (55 % ratio) to exclude background variation.

Threshold: `SSIM_CONFIRM = 0.85` → confirmed duplicate, stage labelled `ssim` or `ssim_photo`.

#### Gate 4 — AKAZE keypoint matching (expensive, only if SSIM < 0.85)

OpenCV AKAZE descriptors with Lowe ratio test (0.75). Good-match ratio = `good_matches / min(keypoints_a, keypoints_b)`.

- **Photo pairs:** centre crop (65 % ratio) used to avoid background keypoints.
- `AKAZE_RATIO = 0.22` — at least 22 % of the sparser keypoint set must match.

If both SSIM and AKAZE fall below their thresholds → `reject`, with the reason string recording both scores.

**Stage labels in the report:**

| `det_stage` | Meaning |
|------------|---------|
| `ssim` | Confirmed by SSIM on full image (scanner pair) |
| `ssim_photo` | Confirmed by SSIM on centre crop (camera pair) |
| `akaze` | Confirmed by AKAZE on full image |
| `akaze_photo` | Confirmed by AKAZE on centre crop |

---

### Stage 5 — Graph Construction & Clustering (`graph.py`)

Confirmed pairs (i, j, score) are added as edges to a `SparseGraph`. Each node retains at most `MAX_EDGES_PER_NODE = 10` strongest-scored neighbours (pruned by score descending). This prevents a single highly-connected page from expanding the cluster transitively into unrelated pages.

Union-Find (DSU with path compression and union-by-rank) groups connected pages into clusters. Only groups with ≥ 2 members are returned as actual duplicate clusters.

---

### Stage 6 — Report Generation (`reporter.py`)

#### Thumbnail saving

All thumbnails are written as 300×400 JPEG tiles (letter-box padded on a 245,245,245 grey canvas). Two sets are produced:
- `clusters/cluster_NNN/` — one thumbnail per member of each duplicate cluster.
- `unique/` — one thumbnail per unique (non-duplicate) page.

#### Cluster type classification

For each cluster, all member pairs are checked in `pair_meta` for their `rel_type`:
- `cross` — members come from different top-level folders.
- `intra` — same folder, different files.
- `infile` — same source file, different pages.

The cluster is assigned the highest-priority type present (cross > intra > infile).

#### JSON Report Structure

`report.json` is written to `media/clusters/<job_id>/report.json`.

```json
{
  "job_id": "string",
  "total_pages": 42,
  "total_duplicates": 10,
  "total_clusters": 4,
  "unique_pages": 32,
  "cross_folder_clusters": 1,
  "within_folder_clusters": 2,
  "within_doc_clusters": 1,

  "clusters": [
    {
      "cluster_id": 1,
      "cluster_dir": "clusters/cluster_001",
      "cluster_type": "cross",
      "cluster_label": "Cross-folder duplicate",
      "size": 3,
      "members": [
        {
          "page_index": 5,
          "source": "folder_a/doc.pdf",
          "page_number": 6,
          "label": "folder_a/doc.pdf · p6",
          "thumb_rel": "<job_id>/clusters/cluster_001/page_00005.jpg",
          "folder": "folder_a",
          "det_stage": "ssim",
          "det_label": "Detected by SSIM",
          "det_color": "#1d9e75",
          "det_score": 0.9123,
          "det_angle": 0.0
        }
      ]
    }
  ],

  "unique_pages_list": [
    {
      "page_index": 0,
      "source": "folder_a/doc.pdf",
      "page_number": 1,
      "label": "folder_a/doc.pdf · p1",
      "thumb_rel": "<job_id>/unique/unique_00000.jpg",
      "rej_stage": "gate1_reject",
      "rej_explain": "Rejected at Gate 1 (Ring hash + Centre hash both different) — content profile too different to be a duplicate",
      "rej_detail": "ring_hamming=9>6 AND centre_hamming=18>14",
      "rej_score": 0.0,
      "rej_color": "#e24b4a"
    }
  ]
}
```

**Rejection stages for unique pages:**

| `rej_stage` | Explanation |
|-------------|-------------|
| `no_candidate` | Never entered any hash bucket with another page — no comparison was made |
| `gate1_reject` | Ring hash AND centre hash both exceeded thresholds |
| `gate2_reject` | wHash Hamming too high (and not a photo pair) |
| `reject` | SSIM and AKAZE both below their confirmation thresholds |

---

## Key Design Decisions

**Why four hashes?** No single hash is robust across all document types. wHash handles aligned scans. dHash handles gradient-heavy content. ring_hash handles rotation. centre_hash handles camera photos where the background dominates the outer region.

**Why inner-60 % ring hash?** Standard ring hashes compute ring means to the image edge. For camera photos the outer ring area is background (table, wall, hand). Restricting to inner 60 % of the radius keeps all 16 rings within the document content area for any realistic camera angle.

**Why a gate cascade instead of running AKAZE on every pair?** AKAZE is CPU-intensive. The hash gates reject > 95 % of non-duplicate candidate pairs with microsecond-level bit operations, so AKAZE runs only on the small fraction that passed SSIM but didn't reach 0.85.

**Why Union-Find for clustering?** Transitive closure via Union-Find is O(n α(n)) ≈ O(n). It correctly handles the case where page A matches B and B matches C (A, B, C form one cluster) without quadratic re-scanning.

**Why sparse graph with MAX_EDGES_PER_NODE = 10?** Without edge pruning, a single template page present in hundreds of documents could pull all of them into one giant cluster. Limiting each node to its 10 strongest edges keeps clusters semantically tight.

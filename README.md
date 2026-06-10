<<<<<<< HEAD
# Dedup Pipeline — Django Web App

A full implementation of the **wHash + MinHash + AKAZE + Graph** dedup pipeline
with a web interface for human verification of detected duplicate pages.

## Quick Start

```bash
# 1. Create a virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
python manage.py runserver

# 4. Open http://127.0.0.1:8000
```

## What it does

1. **Upload** — any mix of: PDFs, DOCX files, images (PNG/JPG/TIFF), or a ZIP of any of these
2. **Pipeline runs** (7 stages, shown live):
   - Ingestion: convert every page to a 512×512 grayscale image
   - Hashing: wHash (Haar LL), dHash (gradient), MinHash (128 projections)
   - Bucketization: LSH-style bucketing (top-16 wHash bits + dHash bits), max 300/bucket
   - Similarity gate: — Hamming → SSIM → AKAZE fallback
   - Sparse graph: nodes=pages, edges=confirmed duplicates, max 10 edges/node
   - Union-Find clustering: O(N·α(N)) path-compressed DSU
   - Reporter: saves thumbnails + report.json
3. **Results** — clusters of duplicate pages with full provenance (source file + page number)
4. **Human verification** — click "Remove" to dismiss a page from a cluster; state persists in report.json

## Supported inputs

| Input type         | How to upload              |
|--------------------|----------------------------|
| Single PDF         | Select the .pdf file       |
| Single DOCX        | Select the .docx file      |
| Single image       | PNG, JPG, TIFF, BMP, WebP  |
| Multiple files     | Select multiple at once    |
| Folder of files    | ZIP the folder, upload .zip |
| Two folders        | ZIP both into one archive  |

## Optional: AKAZE feature matching (Stage 4 fallback)

```bash
pip install opencv-python
```
Without OpenCV, the SSIM zone [0.75–0.90] defaults to "not duplicate" — 
install opencv-python to enable the AKAZE fallback for ambiguous pairs.

## Memory efficiency

- Images are processed page-by-page (streamed), never all loaded at once
- After clustering, PIL image objects are freed from memory
- Only 256×256 JPEG thumbnails are stored on disk (not full-res pages)
- report.json is updated in-place on human verification actions

## Project structure

```
dedup_project/
├── manage.py
├── requirements.txt
├── dedup_project/          Django project settings
│   ├── settings.py
│   └── urls.py
├── dedup_app/
│   ├── views.py            HTTP handlers + job state
│   ├── urls.py
│   ├── pipeline/
│   │   ├── ingestion.py    Stage 1: PDF/DOCX → PIL images
│   │   ├── hashing.py      Stage 2: wHash, dHash, MinHash
│   │   ├── nodes.py        Stage 3: LSH bucketization
│   │   ├── similarity.py   Stage 4: 4-gate similarity
│   │   ├── graph.py        Stages 5-6: sparse graph + Union-Find
│   │   ├── reporter.py     Stage 7: thumbnails + report.json
│   │   └── run_pipeline.py Orchestrator
│   ├── templates/dedup_app/
│   │   ├── base.html
│   │   ├── index.html      Upload form
│   │   ├── progress.html   Live SSE progress
│   │   ├── results.html    Cluster viewer
│   │   └── error.html
│   └── static/dedup_app/css/main.css
└── media/
    ├── uploads/            Raw uploaded files (per job)
    ├── clusters/           Thumbnails + report.json (per job)
    └── tmp/                LibreOffice conversion temp files
```
=======
# DEDUPLICATION
>>>>>>> 

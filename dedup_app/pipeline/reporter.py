import io, json, shutil
from pathlib import Path
from typing import Dict, List, Callable, Optional
from PIL import Image

THUMB_W, THUMB_H = 300, 400

_TYPE_LABELS = {
    'cross': 'Cross-folder duplicate',
    'intra': 'Within-folder duplicate',
    'infile': 'Within-document duplicate',
}
_STAGE_LABELS = {
    'ssim':        'Detected by SSIM',
    'ssim_photo':  'Detected by SSIM (photo mode)',
    'akaze':       'Detected by AKAZE',
    'akaze_photo': 'Detected by AKAZE (photo mode)',
}
_STAGE_COLORS = {
    'ssim':        '#1d9e75',
    'ssim_photo':  '#27ae87',
    'akaze':       '#7f77dd',
    'akaze_photo': '#9b8fe8',
}

# Rejection stage → human explanation
_REJECT_STAGE_EXPLAIN = {
    'gate1_reject': 'Rejected at Gate 1 (Ring hash + Centre hash both different) — content profile too different to be a duplicate',
    'gate2_reject': 'Rejected at Gate 2 (wHash Hamming too high) — structural layout too different',
    'reject':       'Rejected at Gate 4 — SSIM and AKAZE both below threshold',
}

def _label(page):
    return f"{page['source']} · p{page['page_index']+1}"

def _save_thumb(color_bytes, dest):
    img = Image.open(io.BytesIO(color_bytes)).convert('RGB')
    img.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
    canvas = Image.new('RGB', (THUMB_W, THUMB_H), (245,245,245))
    canvas.paste(img, ((THUMB_W-img.width)//2,(THUMB_H-img.height)//2))
    canvas.save(str(dest), 'JPEG', quality=85, optimize=True)
    img.close(); canvas.close()

def _gray_to_bytes(gray):
    buf = io.BytesIO()
    gray.convert('RGB').save(buf,'JPEG',quality=80)
    return buf.getvalue()

def _best_det(page_idx, member_ids, pair_meta):
    best = {'det_stage':'ssim','score':0.0,'angle':0.0}
    best_score = -1.0
    for other in member_ids:
        if other == page_idx: continue
        key  = (min(page_idx,other), max(page_idx,other))
        meta = pair_meta.get(key)
        if meta and meta['score'] > best_score:
            best_score = meta['score']
            best = meta
    return best

def save_clusters(pages, duplicate_clusters, output_dir, job_id,
                  cluster_type_fn=None, pair_meta=None, page_reject=None):
    pair_meta   = pair_meta   or {}
    page_reject = page_reject or {}

    job_dir   = output_dir / job_id
    clust_dir = job_dir / 'clusters'
    uniq_dir  = job_dir / 'unique'
    if job_dir.exists(): shutil.rmtree(job_dir)
    clust_dir.mkdir(parents=True)
    uniq_dir.mkdir(parents=True)

    sorted_clusters = sorted(
        duplicate_clusters.items(), key=lambda kv: len(kv[1]), reverse=True)

    clusters_data = []
    dup_indices   = set()

    for num, (root, members) in enumerate(sorted_clusters, 1):
        cdir  = clust_dir / f'cluster_{num:03d}'
        cdir.mkdir()
        ctype = cluster_type_fn(members) if cluster_type_fn else 'infile'

        mdata = []
        for idx in members:
            page  = pages[idx]
            fname = f'page_{idx:05d}.jpg'
            cb    = page.get('color_bytes') or _gray_to_bytes(page['image'])
            _save_thumb(cb, cdir/fname)

            det = _best_det(idx, members, pair_meta)
            ds  = det.get('det_stage','ssim')
            mdata.append({
                'page_index':  idx,
                'source':      page['source'],
                'page_number': page['page_index']+1,
                'label':       _label(page),
                'thumb_rel':   f"{job_id}/clusters/cluster_{num:03d}/{fname}",
                'folder':      page['source'].split('/')[0] if '/' in page['source'] else 'root',
                'det_stage':   ds,
                'det_label':   _STAGE_LABELS.get(ds, ds.upper()),
                'det_color':   _STAGE_COLORS.get(ds, '#94a3b8'),
                'det_score':   round(det.get('score',0.0), 4),
                'det_angle':   round(det.get('angle',0.0), 1),
            })
            dup_indices.add(idx)

        clusters_data.append({
            'cluster_id':    num,
            'cluster_dir':   f"clusters/cluster_{num:03d}",
            'cluster_type':  ctype,
            'cluster_label': _TYPE_LABELS.get(ctype, ctype),
            'size':          len(members),
            'members':       mdata,
        })

    # Unique pages — with rejection reason
    unique_data = []
    for idx in range(len(pages)):
        if idx in dup_indices: continue
        page  = pages[idx]
        fname = f'unique_{idx:05d}.jpg'
        cb    = page.get('color_bytes') or _gray_to_bytes(page.get('image'))
        if cb: _save_thumb(cb, uniq_dir/fname)

        # Rejection info
        rej = page_reject.get(idx, {})
        rej_stage  = rej.get('stage', 'no_candidate')
        rej_score  = rej.get('score', 0.0)
        rej_reason = rej.get('reason', '')

        # Build human-readable explanation
        if rej_stage == 'no_candidate':
            rej_explain = 'Never became a candidate pair — no similar hash in any bucket'
            rej_color   = '#ef9f27'
        else:
            rej_explain = _REJECT_STAGE_EXPLAIN.get(rej_stage,
                          f'Rejected at {rej_stage}')
            rej_color   = '#e24b4a'

        unique_data.append({
            'page_index':  idx,
            'source':      page['source'],
            'page_number': page['page_index']+1,
            'label':       _label(page),
            'thumb_rel':   f"{job_id}/unique/{fname}",
            'rej_stage':   rej_stage,
            'rej_explain': rej_explain,
            'rej_detail':  rej_reason,
            'rej_score':   round(rej_score, 4),
            'rej_color':   rej_color,
        })

    type_counts = {}
    for cl in clusters_data:
        t = cl['cluster_type']
        type_counts[t] = type_counts.get(t,0)+1

    report = {
        'job_id':                 job_id,
        'total_pages':            len(pages),
        'total_duplicates':       len(dup_indices),
        'total_clusters':         len(clusters_data),
        'unique_pages':           len(unique_data),
        'cross_folder_clusters':  type_counts.get('cross',0),
        'within_folder_clusters': type_counts.get('intra',0),
        'within_doc_clusters':    type_counts.get('infile',0),
        'clusters':               clusters_data,
        'unique_pages_list':      unique_data,
    }
    (job_dir/'report.json').write_text(
        json.dumps(report,indent=2,ensure_ascii=False), encoding='utf-8')
    return report

from pathlib import Path
from typing import List, Dict, Callable, Optional
from .ingestion  import ingest_file
from .hashing    import hash_page
from .nodes      import build_candidate_pairs
from .similarity import compare
from .graph      import build_graph, cluster
from .reporter   import save_clusters

def _folder_of(label):
    parts = label.replace('\\','/').split('/')
    return parts[0] if len(parts)>1 else '__root__'

def _file_of(label):
    return label.split('·')[0].strip()

def _rel_type(pages, i, j):
    fa,fb = _folder_of(pages[i]['source']),_folder_of(pages[j]['source'])
    sa,sb = _file_of(pages[i]['source']),  _file_of(pages[j]['source'])
    if fa!=fb: return 'cross'
    if sa!=sb: return 'intra'
    return 'infile'

def run(file_entries, job_id, clusters_root, tmp_dir, progress_cb=None):
    def _prog(stage, pct):
        if progress_cb: progress_cb(stage, pct)

    tmp_dir.mkdir(parents=True, exist_ok=True)

    _prog('ingestion', 5)
    pages = []
    for entry in file_entries:
        for page in ingest_file(entry['path'], entry['label'], tmp_dir):
            pages.append(page)
    if not pages:
        return {'error': 'No pages could be extracted.'}
    _prog('ingestion', 18)

    _prog('hashing', 20)
    for page in pages:
        hash_page(page)
    _prog('hashing', 33)

    _prog('bucketization', 35)
    candidate_pairs = build_candidate_pairs(pages)
    _prog('bucketization', 48)

    _prog('similarity', 50)
    confirmed   = []   # (i,j,score,rtype,stage,angle)
    # Track best rejection reason per page index for unique/ reporting
    page_reject = {}   # idx → {stage, reason, score}

    total     = len(candidate_pairs)
    all_pairs = sorted(candidate_pairs)

    for k, (i,j) in enumerate(all_pairs):
        result = compare(pages[i], pages[j])
        if result.is_duplicate:
            rtype = _rel_type(pages, i, j)
            confirmed.append((i, j, result.score, rtype, result.stage, result.angle))
        else:
            # Record rejection for both pages (keep worst = most informative)
            for idx in (i, j):
                prev = page_reject.get(idx, {})
                if result.score > prev.get('score', -1):
                    page_reject[idx] = {
                        'stage':  result.stage,
                        'reason': result.reject_reason,
                        'score':  result.score,
                    }
        if k % max(1, total//25) == 0:
            _prog('similarity', 50 + int(30*k/max(1,total)))

    # Free grayscale PIL images
    for p in pages:
        for k in ('image','wh','dh','rh','ch'):
            p.pop(k, None)
    _prog('similarity', 80)

    _prog('clustering', 82)
    graph_triples = [(i,j,s) for i,j,s,_,_,_ in confirmed]
    g        = build_graph(len(pages), graph_triples)
    clusters = cluster(g, len(pages))
    _prog('clustering', 90)

    _prog('reporting', 92)
    pair_meta = {}
    for i,j,score,rtype,stage,angle in confirmed:
        key = (min(i,j), max(i,j))
        pair_meta[key] = {'score':score,'rel_type':rtype,
                          'det_stage':stage,'angle':angle}

    _PRIORITY = {'cross':0,'intra':1,'infile':2}
    def _cluster_type(member_ids):
        best = 'infile'
        for a in range(len(member_ids)):
            for b in range(a+1, len(member_ids)):
                key   = (min(member_ids[a],member_ids[b]),
                         max(member_ids[a],member_ids[b]))
                rtype = pair_meta.get(key,{}).get('rel_type','infile')
                if _PRIORITY.get(rtype,2) < _PRIORITY.get(best,2):
                    best = rtype
        return best

    report = save_clusters(
        pages, clusters, clusters_root, job_id,
        cluster_type_fn=_cluster_type,
        pair_meta=pair_meta,
        page_reject=page_reject,
    )
    for p in pages:
        p.pop('color_bytes', None)

    _prog('done', 100)
    return report

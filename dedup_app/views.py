import json, threading, time, uuid, zipfile
from pathlib import Path
from typing import Dict

from django.conf import settings
from django.http import JsonResponse, StreamingHttpResponse, Http404
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

from .pipeline.run_pipeline import run as run_pipeline

_jobs: Dict[str, dict] = {}

def _set_progress(job_id, stage, pct):
    if job_id in _jobs:
        _jobs[job_id].update({'stage':stage,'progress':pct,'status':'running'})

MEDIA_ROOT   = Path(settings.MEDIA_ROOT)
UPLOADS_DIR  = MEDIA_ROOT / 'uploads'
CLUSTERS_DIR = MEDIA_ROOT / 'clusters'
TMP_DIR      = MEDIA_ROOT / 'tmp'
for _d in (UPLOADS_DIR, CLUSTERS_DIR, TMP_DIR):
    _d.mkdir(parents=True, exist_ok=True)

PIPELINE_STAGES = [
    {'key':'ingestion',     'label':'1 · Ingestion'},
    {'key':'hashing',       'label':'2 · Hashing'},
    {'key':'bucketization', 'label':'3 · Bucketization'},
    {'key':'similarity',    'label':'4 · Similarity Gate'},
    {'key':'clustering',    'label':'5-6 · Graph & Clustering'},
    {'key':'reporting',     'label':'7 · Reporter'},
]

MODES = {
    'single_file':    {'label':'Single File',       'desc':'Find duplicates within one PDF or DOCX'},
    'two_files':      {'label':'Two Files',          'desc':'Compare two documents against each other'},
    'single_folder':  {'label':'Single Folder (ZIP)','desc':'Find duplicates across all files in a folder'},
    'two_folders':    {'label':'Two Folders (ZIP×2)','desc':'Cross-compare two folder collections'},
    'multi_files':    {'label':'Multiple Files',     'desc':'Upload any mix of files for cross-dedup'},
}

EXTS = {'.pdf','.docx','.doc','.png','.jpg','.jpeg','.tiff','.tif','.bmp','.webp'}

def _save_file(f, dest_dir):
    p = dest_dir / f.name
    with open(p,'wb') as out:
        for chunk in f.chunks(): out.write(chunk)
    return p

def _extract_zip(zip_path, dest_dir, folder_label=None):
    entries = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith('/'): continue
            if Path(name).suffix.lower() not in EXTS: continue
            label = (folder_label + '/' + name) if folder_label else name
            target = dest_dir / (label.replace('/','_').replace('\\','_'))
            with zf.open(name) as src, open(target,'wb') as dst:
                dst.write(src.read())
            entries.append({'path':str(target),'label':label})
    return entries

# ── Views ─────────────────────────────────────────────────────────────────

def index(request):
    return render(request, 'dedup_app/index.html', {'modes': MODES})

def upload(request):
    if request.method != 'POST':
        return redirect('index')

    mode = request.POST.get('mode','multi_files')
    job_id = uuid.uuid4().hex
    job_dir = UPLOADS_DIR / job_id
    job_dir.mkdir(parents=True)

    entries = []
    files = request.FILES.getlist('files')

    if not files:
        return render(request,'dedup_app/index.html',
            {'modes':MODES,'error':'No files received. Please select files before submitting.'})

    if mode == 'single_file':
        f = files[0]
        p = _save_file(f, job_dir)
        entries = [{'path':str(p),'label':f.name}]

    elif mode == 'two_files':
        for f in files[:2]:
            p = _save_file(f, job_dir)
            entries.append({'path':str(p),'label':f.name})

    elif mode == 'single_folder':
        f = files[0]
        p = _save_file(f, job_dir)
        if p.suffix.lower() == '.zip':
            entries = _extract_zip(p, job_dir, folder_label=p.stem)
        else:
            entries = [{'path':str(p),'label':f.name}]

    elif mode == 'two_folders':
        for i, f in enumerate(files[:2]):
            p = _save_file(f, job_dir)
            label = f'folder{i+1}'
            if p.suffix.lower() == '.zip':
                entries.extend(_extract_zip(p, job_dir, folder_label=label))
            else:
                entries.append({'path':str(p),'label':f'{label}/{f.name}'})

    else:  # multi_files
        for f in files:
            p = _save_file(f, job_dir)
            if p.suffix.lower() == '.zip':
                entries.extend(_extract_zip(p, job_dir, folder_label=p.stem))
            else:
                entries.append({'path':str(p),'label':f.name})

    if not entries:
        return render(request,'dedup_app/index.html',
            {'modes':MODES,'error':'No supported files found in your upload.'})

    _jobs[job_id] = {'status':'queued','progress':0,'stage':'queued',
                     'report':None,'error':None,'mode':mode,
                     'file_count':len(entries)}

    def _worker():
        try:
            report = run_pipeline(
                file_entries=entries, job_id=job_id,
                clusters_root=CLUSTERS_DIR, tmp_dir=TMP_DIR/job_id,
                progress_cb=lambda s,p: _set_progress(job_id,s,p),
            )
            if 'error' in report:
                _jobs[job_id].update({'status':'error','error':report['error']})
            else:
                _jobs[job_id].update({'status':'done','report':report})
        except Exception:
            import traceback
            _jobs[job_id].update({'status':'error','error':traceback.format_exc()})

    threading.Thread(target=_worker, daemon=True).start()
    return redirect('progress', job_id=job_id)

def progress(request, job_id):
    if request.headers.get('Accept') == 'text/event-stream':
        def _sse():
            while True:
                job = _jobs.get(job_id)
                if not job:
                    yield 'data: {"status":"error","error":"Job not found"}\n\n'
                    break
                yield f"data: {json.dumps({'status':job['status'],'stage':job['stage'],'progress':job['progress']})}\n\n"
                if job['status'] in ('done','error'): break
                time.sleep(0.5)
        r = StreamingHttpResponse(_sse(), content_type='text/event-stream')
        r['Cache-Control'] = 'no-cache'
        return r
    if job_id not in _jobs:
        raise Http404
    job = _jobs[job_id]
    return render(request,'dedup_app/progress.html',{
        'job_id':job_id,'stages':PIPELINE_STAGES,
        'mode': MODES.get(job.get('mode',''),''),
        'file_count': job.get('file_count',0),
    })

def results(request, job_id):
    job = _jobs.get(job_id)
    if not job:
        rp = CLUSTERS_DIR/job_id/'report.json'
        if not rp.exists(): raise Http404
        report = json.loads(rp.read_text())
    else:
        if job['status'] == 'error':
            return render(request,'dedup_app/error.html',{'error':job['error']})
        if job['status'] != 'done':
            return redirect('progress', job_id=job_id)
        report = job['report']
    return render(request,'dedup_app/results.html',{
        'report':report,'job_id':job_id,'media_url':settings.MEDIA_URL,
    })

@require_POST
def delete_page(request, job_id, cluster_id, page_index):
    cdir = CLUSTERS_DIR/job_id/'clusters'/f'cluster_{cluster_id:03d}'
    for f in cdir.glob(f'page_{page_index:05d}.*'):
        f.unlink(missing_ok=True)
    rp = CLUSTERS_DIR/job_id/'report.json'
    if rp.exists():
        report = json.loads(rp.read_text())
        for cl in report.get('clusters',[]):
            if cl['cluster_id'] == cluster_id:
                cl['members'] = [m for m in cl['members'] if m['page_index']!=page_index]
                cl['size'] = len(cl['members'])
                break
        rp.write_text(json.dumps(report,indent=2))
    return JsonResponse({'ok':True})

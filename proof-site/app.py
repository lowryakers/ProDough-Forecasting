import os
import io
import openpyxl
from flask import (Flask, render_template, request, redirect,
                   url_for, flash, jsonify, send_file)

import proof_engine

app = Flask(__name__)
app.secret_key = 'prodough-proof-site-2024'

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTS = {'.pdf', '.ai', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.eps'}


@app.template_filter('basename_filter')
def basename_filter(path):
    return os.path.basename(path) if path else ''


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('proof.html', jobs=proof_engine.list_jobs())


@app.route('/upload', methods=['POST'])
def upload():
    artwork_files = request.files.getlist('artwork')
    gtin_file     = request.files.get('gtin_list')

    if not artwork_files or all(f.filename == '' for f in artwork_files):
        flash('Please select at least one artwork file.', 'danger')
        return redirect(url_for('index'))

    # Parse optional GTIN reference list
    gtin_rows = []
    if gtin_file and gtin_file.filename:
        try:
            gtin_rows = _parse_gtin_list(gtin_file.read(), gtin_file.filename)
        except Exception as exc:
            flash(f'Could not read GTIN list ({exc}) — proceeding without GTIN cross-check.', 'warning')

    job_id  = proof_engine.create_job([f.filename for f in artwork_files if f.filename])
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    saved, skipped = [], []
    for f in artwork_files:
        if not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXTS:
            skipped.append(f.filename)
            continue
        safe = _safe_name(f.filename)
        dest = os.path.join(job_dir, safe)
        f.save(dest)
        saved.append(dest)

    if skipped:
        flash(f'Skipped unsupported file(s): {", ".join(skipped)}', 'warning')
    if not saved:
        flash('No supported artwork files were uploaded.', 'danger')
        return redirect(url_for('index'))

    proof_engine.start_job(job_id, saved, gtin_rows, job_dir)
    return redirect(url_for('result', job_id=job_id))


@app.route('/result/<job_id>')
def result(job_id):
    job = proof_engine.get_job(job_id)
    if not job:
        flash('Job not found.', 'danger')
        return redirect(url_for('index'))
    return render_template('result.html', job=job, job_id=job_id)


# ── API ───────────────────────────────────────────────────────────────────────

@app.route('/api/status/<job_id>')
def api_status(job_id):
    job = proof_engine.get_job(job_id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'status':       job['status'],
        'progress':     job['progress'],
        'current_file': job['current_file'],
        'error':        job['error'],
    })


@app.route('/api/result/<job_id>')
def api_result(job_id):
    job = proof_engine.get_job(job_id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    return jsonify(job)


@app.route('/image/<job_id>/<path:filename>')
def serve_image(job_id, filename):
    job_dir  = os.path.realpath(os.path.join(UPLOAD_DIR, job_id))
    img_path = os.path.realpath(os.path.join(job_dir, filename))
    if not img_path.startswith(job_dir):
        return 'Forbidden', 403
    if not os.path.exists(img_path):
        return 'Not found', 404
    return send_file(img_path, mimetype='image/png')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    keep = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- ')
    return ''.join(c if c in keep else '_' for c in name)


def _parse_gtin_list(data: bytes, filename: str) -> list:
    rows = []
    if filename.lower().endswith('.csv'):
        import csv
        reader = csv.DictReader(io.TextIOWrapper(io.BytesIO(data), errors='replace'))
        for row in reader:
            rows.append({k.strip().lower(): v.strip() for k, v in row.items()})
    else:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        for ws in wb.worksheets:
            headers = None
            for row in ws.iter_rows(values_only=True):
                if headers is None:
                    if row and any(row):
                        headers = [str(c).strip().lower() if c else '' for c in row]
                    continue
                if not any(row):
                    continue
                d = dict(zip(headers, row))
                gtin  = d.get('gtin/barcode#') or d.get('gtin') or d.get('barcode') or d.get('upc') or ''
                flavor = d.get('flavor') or d.get('product name') or d.get('name') or ''
                sku   = d.get('sku') or ''
                rows.append({
                    'gtin':   str(gtin).strip().replace(' ', ''),
                    'flavor': str(flavor).strip().lower(),
                    'sku':    str(sku).strip(),
                })
    return [r for r in rows if r.get('gtin') and r['gtin'] not in ('', 'nan')]

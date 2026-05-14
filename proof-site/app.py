import os
import io
import json
import openpyxl
from datetime import datetime
from flask import (Flask, render_template, request, redirect,
                   url_for, flash, jsonify, send_file)

import proof_engine

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'prodough-proof-site-2024-local')
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR      = os.path.join(BASE_DIR, 'uploads')
GTIN_STORE_PATH = os.path.join(UPLOAD_DIR, 'gtin_store.json')
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTS = {'.pdf', '.ai', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.eps'}

CHECK_LABELS = {
    'gtin':     'GTIN / Barcode',
    'nfp':      'Front Call-Out vs NFP',
    'eyemark':  'Eyemark Contrast',
    'spelling': 'Spelling / Brand Name',
    'fda':      'FDA Audit Risk',
}


@app.template_filter('basename_filter')
def basename_filter(path):
    return os.path.basename(path) if path else ''


# ── GTIN store ────────────────────────────────────────────────────────────────

def _load_gtin_store():
    if os.path.exists(GTIN_STORE_PATH):
        try:
            with open(GTIN_STORE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _save_gtin_store(rows: list, source_filename: str) -> dict:
    for row in rows:
        gtin = str(row.get('gtin', ''))
        if not gtin:
            row['_valid'] = False
            row['_error'] = 'Missing GTIN'
        elif not gtin.isdigit():
            row['_valid'] = False
            row['_error'] = 'Non-numeric characters'
        elif len(gtin) != 12:
            row['_valid'] = False
            row['_error'] = f'{len(gtin)} digits (expect 12)'
        else:
            row['_valid'] = True
            row['_error'] = None
    store = {
        'uploaded_at': datetime.now().isoformat(),
        'filename': source_filename,
        'row_count': len(rows),
        'error_count': sum(1 for r in rows if not r.get('_valid')),
        'rows': rows,
    }
    with open(GTIN_STORE_PATH, 'w') as f:
        json.dump(store, f)
    return store


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('proof.html', jobs=proof_engine.list_jobs())


@app.route('/gtin')
def gtin_page():
    store = _load_gtin_store()
    return render_template('gtin.html', store=store)


@app.route('/upload', methods=['POST'])
def upload():
    artwork_files = request.files.getlist('artwork')
    gtin_file     = request.files.get('gtin_list')

    if not artwork_files or all(f.filename == '' for f in artwork_files):
        flash('Please select at least one artwork file.', 'danger')
        return redirect(url_for('index'))

    if not gtin_file or not gtin_file.filename:
        flash('Please upload the Master ProDough SKU & GTIN List — it is required for the GTIN/barcode check.', 'danger')
        return redirect(url_for('index'))

    try:
        gtin_rows = _parse_gtin_list(gtin_file.read(), gtin_file.filename)
        if not gtin_rows:
            flash('The GTIN list uploaded appears to be empty or unreadable. Check that it is the correct file.', 'danger')
            return redirect(url_for('index'))
        _save_gtin_store(gtin_rows, gtin_file.filename)
    except Exception as exc:
        flash(f'Could not read the GTIN list: {exc}. Upload a valid .xlsx or .csv file.', 'danger')
        return redirect(url_for('index'))

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


@app.route('/summary/<job_id>')
def summary(job_id):
    job = proof_engine.get_job(job_id)
    if not job:
        flash('Job not found.', 'danger')
        return redirect(url_for('index'))
    if job['status'] != 'done':
        return redirect(url_for('result', job_id=job_id))
    return render_template('summary.html', job=job, job_id=job_id)


@app.route('/viewer/<job_id>')
def viewer(job_id):
    job = proof_engine.get_job(job_id)
    if not job:
        flash('Job not found.', 'danger')
        return redirect(url_for('index'))

    verify_map = {}
    for r in job.get('results', []):
        items = []
        for check_name, check in r.get('checks', {}).items():
            for issue in check.get('issues', []):
                msg = issue.get('message', '')
                if 'verify' in msg.lower() or 'manually' in msg.lower():
                    items.append({
                        'check_label': CHECK_LABELS.get(check_name, check_name),
                        'severity': issue['severity'],
                        'message': msg,
                    })
            for note in check.get('notes', []):
                if 'verify' in note.lower() or 'manually' in note.lower():
                    items.append({
                        'check_label': CHECK_LABELS.get(check_name, check_name),
                        'severity': 'info',
                        'message': note,
                    })
        verify_map[r['filename']] = items

    return render_template('viewer.html', job=job, job_id=job_id, verify_map=verify_map)


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


@app.route('/api/dismiss/<job_id>', methods=['POST'])
def api_dismiss(job_id):
    data = request.get_json()
    if not data:
        return jsonify({'error': 'no data'}), 400
    ok = proof_engine.set_dismissal(
        job_id,
        data.get('filename', ''),
        data.get('check', ''),
        int(data.get('index', 0)),
        bool(data.get('dismissed', True)),
    )
    if not ok:
        return jsonify({'error': 'job not found'}), 404
    return jsonify({'ok': True})


@app.route('/report/<job_id>')
def download_report(job_id):
    job = proof_engine.get_job(job_id)
    if not job or job['status'] != 'done':
        flash('Report not ready — job must complete first.', 'danger')
        return redirect(url_for('index'))
    buf = _generate_report(job)
    fname = f'ProDough_Proof_{job_id}.xlsx'
    return send_file(
        buf, download_name=fname, as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


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
                gtin   = d.get('gtin/barcode#') or d.get('gtin') or d.get('barcode') or d.get('upc') or ''
                flavor = d.get('flavor') or d.get('product name') or d.get('name') or ''
                sku    = d.get('sku') or ''
                rows.append({
                    'gtin':   str(gtin).strip().replace(' ', ''),
                    'flavor': str(flavor).strip().lower(),
                    'sku':    str(sku).strip(),
                })
    return [r for r in rows if r.get('gtin') and r['gtin'] not in ('', 'nan')]


def _generate_report(job: dict) -> io.BytesIO:
    from openpyxl.styles import Font, PatternFill, Alignment

    dismissals = job.get('dismissals', {})

    def is_dismissed(filename, check_name, idx):
        return dismissals.get(f"{filename}|{check_name}|{idx}", False)

    hdr_font  = Font(bold=True, color='FFFFFF')
    hdr_blue  = PatternFill('solid', fgColor='1A56A0')
    hdr_gray  = PatternFill('solid', fgColor='6C757D')
    fill_crit = PatternFill('solid', fgColor='FFD0D0')
    fill_warn = PatternFill('solid', fgColor='FFF3CD')
    fill_ok   = PatternFill('solid', fgColor='D4EDDA')
    wrap      = Alignment(wrap_text=True, vertical='top')
    center    = Alignment(horizontal='center', vertical='top')

    wb = openpyxl.Workbook()

    # ── Sheet 1: Summary ──────────────────────────────────────────────────────
    ws1 = wb.active
    ws1.title = 'Summary'
    ws1.merge_cells('A1:E1')
    ws1['A1'] = 'ProDough Artwork Proof Report'
    ws1['A1'].font = Font(bold=True, size=14)
    ws1['A2'] = f'Job: {job["id"]}'
    ws1['B2'] = f'Date: {job["created"][:10]}'
    ws1['C2'] = f'Files proofed: {len(job["results"])}'
    ws1.append([])
    for col, hdr in enumerate(['File', 'Overall Status', 'Critical', 'Warnings', 'Notes'], 1):
        c = ws1.cell(row=4, column=col, value=hdr)
        c.font = hdr_font
        c.fill = hdr_blue
        c.alignment = center

    for r in job['results']:
        ws1.append([r['filename'], r.get('severity', 'error').upper(),
                    r.get('critical_count', 0), r.get('warning_count', 0), r.get('info_count', 0)])
        sev = r.get('severity', 'error')
        f = fill_crit if sev == 'critical' else fill_warn if sev == 'warning' else fill_ok if sev == 'clean' else None
        if f:
            for col in range(1, 6):
                ws1.cell(row=ws1.max_row, column=col).fill = f

    ws1.column_dimensions['A'].width = 45
    for col in ['B', 'C', 'D', 'E']:
        ws1.column_dimensions[col].width = 14

    # ── Sheet 2: Issues to Fix (non-dismissed) ────────────────────────────────
    ws2 = wb.create_sheet('Issues to Fix')
    for col, hdr in enumerate(['File', 'Check', 'Severity', 'Issue / Required Action'], 1):
        c = ws2.cell(row=1, column=col, value=hdr)
        c.font = hdr_font
        c.fill = hdr_blue
        c.alignment = center

    row_idx = 2
    for r in job['results']:
        for check_name, check in r.get('checks', {}).items():
            for i, issue in enumerate(check.get('issues', [])):
                if is_dismissed(r['filename'], check_name, i):
                    continue
                sev = issue['severity']
                vals = [r['filename'], CHECK_LABELS.get(check_name, check_name),
                        sev.upper(), issue['message']]
                for col, val in enumerate(vals, 1):
                    c = ws2.cell(row=row_idx, column=col, value=val)
                    c.fill = fill_crit if sev == 'critical' else fill_warn if sev == 'warning' else PatternFill()
                    c.alignment = wrap
                row_idx += 1

    ws2.column_dimensions['A'].width = 40
    ws2.column_dimensions['B'].width = 22
    ws2.column_dimensions['C'].width = 12
    ws2.column_dimensions['D'].width = 80

    # ── Sheet 3: Reviewed / Dismissed ─────────────────────────────────────────
    if dismissals:
        ws3 = wb.create_sheet('Reviewed OK')
        for col, hdr in enumerate(['File', 'Check', 'Severity', 'Issue (Reviewed — No Action Needed)'], 1):
            c = ws3.cell(row=1, column=col, value=hdr)
            c.font = hdr_font
            c.fill = hdr_gray
            c.alignment = center

        row_idx = 2
        for r in job['results']:
            for check_name, check in r.get('checks', {}).items():
                for i, issue in enumerate(check.get('issues', [])):
                    if not is_dismissed(r['filename'], check_name, i):
                        continue
                    vals = [r['filename'], CHECK_LABELS.get(check_name, check_name),
                            issue['severity'].upper(), issue['message']]
                    for col, val in enumerate(vals, 1):
                        ws3.cell(row=row_idx, column=col, value=val).alignment = wrap
                    row_idx += 1

        ws3.column_dimensions['A'].width = 40
        ws3.column_dimensions['B'].width = 22
        ws3.column_dimensions['C'].width = 12
        ws3.column_dimensions['D'].width = 80

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf

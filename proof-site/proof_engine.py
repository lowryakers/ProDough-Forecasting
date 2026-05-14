"""
ProDough Artwork Proofing Engine
Processes packaging PDFs through 5 checks:
  1. GTIN/barcode detection
  2. Front call-out vs Nutrition Facts Panel consistency
  3. Eyemark contrast
  4. Spelling / brand-name errors
  5. FDA compliance flags
"""
import os
import re
import json
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')

# ── In-memory job store ───────────────────────────────────────────────────────

_jobs: dict = {}
_jobs_lock = threading.Lock()


def create_job(filenames: list) -> str:
    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            'id': job_id,
            'status': 'pending',
            'created': datetime.now().isoformat(),
            'filenames': filenames,
            'progress': 0,
            'current_file': '',
            'results': [],
            'summary': {},
            'error': None,
            'dismissals': {},
        }
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        return dict(_jobs[job_id]) if job_id in _jobs else None


def list_jobs() -> list:
    with _jobs_lock:
        return [dict(j) for j in sorted(_jobs.values(), key=lambda x: x['created'], reverse=True)]


def set_dismissal(job_id: str, filename: str, check_name: str,
                  issue_index: int, dismissed: bool) -> bool:
    with _jobs_lock:
        if job_id not in _jobs:
            return False
        key = f"{filename}|{check_name}|{issue_index}"
        if dismissed:
            _jobs[job_id]['dismissals'][key] = True
        else:
            _jobs[job_id]['dismissals'].pop(key, None)
    _save_job_to_disk(job_id)
    return True


def _update_job(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


def start_job(job_id: str, pdf_paths: list, gtin_rows: list, work_dir: str):
    t = threading.Thread(
        target=_process_job,
        args=(job_id, pdf_paths, gtin_rows, work_dir),
        daemon=True,
    )
    t.start()


# ── Disk persistence ──────────────────────────────────────────────────────────

def _save_job_to_disk(job_id: str):
    with _jobs_lock:
        if job_id not in _jobs:
            return
        job = dict(_jobs[job_id])
    job_file = os.path.join(_UPLOAD_DIR, job_id, 'job.json')
    try:
        with open(job_file, 'w') as f:
            json.dump(job, f)
    except Exception:
        pass


def load_jobs_from_disk():
    """Load previously saved jobs from disk. Called once at startup."""
    if not os.path.exists(_UPLOAD_DIR):
        return
    for entry in os.listdir(_UPLOAD_DIR):
        job_file = os.path.join(_UPLOAD_DIR, entry, 'job.json')
        if not os.path.exists(job_file):
            continue
        try:
            with open(job_file) as f:
                job = json.load(f)
            with _jobs_lock:
                if job.get('id') and job['id'] not in _jobs:
                    _jobs[job['id']] = job
        except Exception:
            pass


# Load history on import
load_jobs_from_disk()


# ── Main processing loop ──────────────────────────────────────────────────────

def _process_job(job_id: str, pdf_paths: list, gtin_rows: list, work_dir: str):
    _update_job(job_id, status='running')
    results = []
    total = len(pdf_paths)

    try:
        for i, pdf_path in enumerate(pdf_paths):
            fname = os.path.basename(pdf_path)
            _update_job(job_id, current_file=fname, progress=int(i / total * 90))
            try:
                result = _proof_single(pdf_path, gtin_rows, work_dir)
            except Exception as exc:
                result = {
                    'filename': fname,
                    'error': str(exc),
                    'checks': {},
                    'severity': 'error',
                    'critical_count': 0,
                    'warning_count': 0,
                    'info_count': 0,
                    'img_web': None,
                }
            results.append(result)

        summary = _build_summary(results)
        _update_job(job_id, status='done', progress=100,
                    results=results, summary=summary, current_file='')
    except Exception as exc:
        _update_job(job_id, status='error', error=str(exc), progress=0)

    _save_job_to_disk(job_id)


# ── Film vs pouch detection ───────────────────────────────────────────────────

_FILM_KEYWORDS  = {'stick', 'sachet', 'flow', 'rollstock', 'film', 'sleeve',
                   'wrapper', 'wrap', 'stickpack', 'stick_pack', 'stick-pack'}
_POUCH_KEYWORDS = {'pouch', 'bag', 'zip', 'mylar', 'doypack', 'doypak',
                   'standup', 'stand_up', 'stand-up', 'canister', 'jar',
                   'bottle', 'tub', 'container'}


def _is_film_rollstock(fname: str, ocr_text: str = '') -> bool:
    """Return True only when the design is clearly identified as film/rollstock.
    Defaults to False (skip eyemark check) when format is ambiguous — the check
    is meaningless on pouches and should not produce false positives.
    """
    name = fname.lower()
    text = ocr_text.lower()

    # Explicit pouch/bag indicators → not film
    for kw in _POUCH_KEYWORDS:
        if kw in name:
            return False

    # Explicit film/stick-pack indicators → apply eyemark
    for kw in _FILM_KEYWORDS:
        if kw in name:
            return True

    # Fall back to OCR text hints
    if any(kw in text for kw in _POUCH_KEYWORDS):
        return False
    if any(kw in text for kw in _FILM_KEYWORDS):
        return True

    # Cannot determine — default to skipping so pouches don't generate false positives.
    # Rename the file to include "stick", "sachet", or "film" to enable this check.
    return False


# ── Single-file proofing ──────────────────────────────────────────────────────

def _proof_single(pdf_path: str, gtin_rows: list, work_dir: str) -> dict:
    fname = os.path.basename(pdf_path)
    stem = Path(pdf_path).stem

    # ── Convert PDF → PNG ────────────────────────────────────────────────────
    img_prefix = os.path.join(work_dir, stem)
    subprocess.run(
        ['pdftoppm', '-r', '250', '-png', '-singlefile', pdf_path, img_prefix],
        capture_output=True, check=True, timeout=120,
    )
    img_path = img_prefix + '.png'
    if not os.path.exists(img_path):
        candidates = sorted(
            f for f in os.listdir(work_dir)
            if f.startswith(stem) and f.endswith('.png')
        )
        if not candidates:
            raise FileNotFoundError(f'pdftoppm produced no PNG for {fname}')
        img_path = os.path.join(work_dir, candidates[0])

    # ── OCR ──────────────────────────────────────────────────────────────────
    ocr_text = ''
    try:
        r = subprocess.run(
            ['tesseract', img_path, 'stdout', '--oem', '3', '--psm', '3', '-l', 'eng'],
            capture_output=True, text=True, timeout=120,
        )
        ocr_text = r.stdout
    except Exception:
        pass

    # ── Load image for visual checks ─────────────────────────────────────────
    img = None
    if PIL_AVAILABLE:
        try:
            img = Image.open(img_path).convert('RGB')
        except Exception:
            pass

    is_film = _is_film_rollstock(fname, ocr_text)

    checks = {
        'gtin':     _check_gtin(ocr_text, fname, gtin_rows),
        'nfp':      _check_nfp(ocr_text),
        'eyemark':  _check_eyemark(img, is_film, fname),
        'spelling': _check_spelling(ocr_text, fname),
        'fda':      _check_fda(ocr_text, fname),
    }

    all_issues = [i for c in checks.values() for i in c.get('issues', [])]
    crit  = [i for i in all_issues if i['severity'] == 'critical']
    warns = [i for i in all_issues if i['severity'] == 'warning']
    infos = [i for i in all_issues if i['severity'] == 'info']

    if crit:
        severity = 'critical'
    elif warns:
        severity = 'warning'
    elif infos:
        severity = 'info'
    else:
        severity = 'clean'

    return {
        'filename': fname,
        'img_path': img_path,
        'img_web': img_path,
        'ocr_preview': ocr_text[:2000],
        'checks': checks,
        'severity': severity,
        'critical_count': len(crit),
        'warning_count': len(warns),
        'info_count': len(infos),
        'error': None,
    }


# ── Check 1: GTIN / barcode ───────────────────────────────────────────────────

def _check_gtin(ocr_text: str, fname: str, gtin_rows: list) -> dict:
    issues, notes = [], []

    raw12 = re.findall(r'\b(\d{12})\b', ocr_text)
    partial = re.findall(r'\b(\d{5,7})\s+(\d{4,7})\b', ocr_text)
    for a, b in partial:
        combined = a + b
        if len(combined) in (11, 12):
            raw12.append(combined)

    found = list({g for g in raw12 if g[:3] in ('850', '840', '860', '870', '880', '890', '012', '075', '049')})
    if not found:
        found = list(set(raw12))

    if found:
        notes.append(f'Detected GTIN(s) via OCR: {", ".join(found)}')
    else:
        notes.append('No 12-digit GTIN detected via OCR. Barcodes set as paths/outlines may not OCR. Verify the barcode number manually.')

    if gtin_rows and found:
        gtin_lookup = {str(r.get('gtin', '')).strip(): str(r.get('flavor', '')).strip().lower()
                       for r in gtin_rows if r.get('gtin')}
        fname_lower = fname.lower()
        for gtin in found:
            if gtin in gtin_lookup:
                expected_flavor = gtin_lookup[gtin]
                if expected_flavor and expected_flavor not in fname_lower:
                    issues.append({
                        'severity': 'critical',
                        'message': (
                            f'GTIN {gtin} in the master list belongs to "{expected_flavor}", '
                            f'but the artwork file is named "{fname}". '
                            'This may be the wrong barcode — verify against the master SKU list.'
                        ),
                    })
            else:
                issues.append({
                    'severity': 'warning',
                    'message': f'GTIN {gtin} was not found in the uploaded master list. Verify it belongs to this SKU.',
                })

    return {'found_gtins': found, 'issues': issues, 'notes': notes}


# ── Check 2: Front call-outs vs NFP ──────────────────────────────────────────

def _check_nfp(ocr_text: str) -> dict:
    issues, notes = [], []
    tl = ocr_text.lower()

    has_nfp = 'nutrition facts' in tl or 'supplement facts' in tl
    if not has_nfp:
        notes.append(
            'Nutrition Facts (or Supplement Facts) panel not detected via OCR. '
            'Print-ready PDFs with text converted to outlines will not OCR — '
            'verify manually that the NFP is present and correctly formatted.'
        )

    cal_hits = re.findall(r'calories\s+(\d+)|(\d+)\s+calories', tl)
    calories = sorted({int(a or b) for a, b in cal_hits if 30 <= int(a or b) <= 800})

    prot_hits = re.findall(r'protein\s+(\d+)\s*g|(\d+)\s*g\s+protein', tl)
    proteins = sorted({int(a or b) for a, b in prot_hits if 0 < int(a or b) < 120})

    nw_hits = re.findall(r'net\s*wt\.?\s*([\d.]+)\s*g', tl)

    if len(set(calories)) > 1:
        diff = max(calories) - min(calories)
        if diff > 5:
            issues.append({
                'severity': 'warning',
                'message': (
                    f'Multiple calorie values detected ({", ".join(str(c) for c in calories)} cal). '
                    'If the front call-out and NFP show different values, this is an FDA labeling violation.'
                ),
            })

    if len(set(proteins)) > 1:
        if max(proteins) - min(proteins) > 1:
            issues.append({
                'severity': 'warning',
                'message': (
                    f'Multiple protein values detected ({", ".join(str(p) for p in proteins)}g). '
                    'Front call-out must match the NFP protein grams.'
                ),
            })

    if re.search(r'0\s*g\s+added\s+sugar', tl):
        if not re.search(r'added\s+sugars?\s+0\s*g|added\s+sugars?\s*\n?\s*0', tl):
            notes.append(
                '"0G Added Sugar" front claim detected. '
                'Verify the NFP shows 0g for Added Sugars.'
            )

    return {
        'has_nfp': has_nfp,
        'calories': calories,
        'proteins': proteins,
        'net_weights': nw_hits,
        'issues': issues,
        'notes': notes,
    }


# ── Check 3: Eyemark contrast ─────────────────────────────────────────────────

def _check_eyemark(img, is_film: bool = False, fname: str = '') -> dict:
    issues, notes = [], []

    if not is_film:
        notes.append(
            'Eyemark check skipped — this design does not appear to be film/rollstock. '
            'Eyemark registration marks are only required for film/rollstock (stick packs, sachets, flow wrap). '
            'To enable this check, include "stick", "sachet", "film", or "rollstock" in the filename.'
        )
        return {'issues': issues, 'notes': notes, 'contrast': None, 'skipped': True}

    if img is None:
        notes.append('Image unavailable — eyemark contrast check skipped.')
        return {'issues': issues, 'notes': notes, 'contrast': None}

    w, h = img.size
    mw = max(1, int(w * 0.10))
    mh = max(1, int(h * 0.08))

    def luma(region):
        pixels = list(region.getdata())
        if not pixels:
            return 128
        return sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels) / len(pixels)

    mark_region    = img.crop((w - mw, h - mh, w, h))
    surround_region = img.crop((w - mw * 3, h - mh * 3, w - mw, h - mh))

    mark_luma = luma(mark_region)
    surr_luma = luma(surround_region)
    contrast  = abs(mark_luma - surr_luma)

    mark_desc = 'dark' if mark_luma < 80 else ('light' if mark_luma > 175 else 'medium')
    surr_desc = 'dark' if surr_luma < 80 else ('light' if surr_luma > 175 else 'medium')

    notes.append(
        f'Bottom-right region — eyemark brightness: {mark_luma:.0f} ({mark_desc}), '
        f'surrounding brightness: {surr_luma:.0f} ({surr_desc}), contrast delta: {contrast:.0f}.'
    )

    if contrast < 55:
        issues.append({
            'severity': 'warning',
            'message': (
                f'Low eyemark contrast ({mark_desc} mark on {mark_desc} background, delta {contrast:.0f}). '
                'The bagger photo-eye may not reliably detect the film position. '
                'Confirm eyemark color with the print supplier before going to press.'
            ),
        })
    elif contrast < 100:
        issues.append({
            'severity': 'info',
            'message': (
                f'Borderline eyemark contrast (delta {contrast:.0f}). '
                'Verify with print supplier that the eyemark is detectable under production conditions.'
            ),
        })
    else:
        notes.append(f'Eyemark contrast appears adequate (delta {contrast:.0f}).')

    return {'issues': issues, 'notes': notes, 'contrast': round(contrast)}


# ── Check 4: Spelling / brand name ───────────────────────────────────────────

_MISSPELLINGS = {
    # Use word boundaries so "prodoughshop" and "@prodoughshop" are never matched.
    # The (?!®|\(r\)) lookahead prevents flagging correctly formatted ProDough® text.
    r'\bprodough\b(?!®|\(r\))': 'ProDough® (verify capital P, capital D, no space, and ® symbol)',
    r'pro\s+dough':              'ProDough (should be one word, no space)',
    r'\bcheescake\b':            'cheesecake',
    r'\bbanna\b':                'banana',
    r'\bchoclate\b':             'chocolate',
    r'\bvanila\b':               'vanilla',
    r'\bcarmel\b':               'caramel',
    r'\bcinamon\b':              'cinnamon',
    r'\bstrwberry\b':            'strawberry',
    r'\brasberry\b':             'raspberry',
    r'\braspbery\b':             'raspberry',
    r'\bprotien\b':              'protein',
    r'\bingrediant':             'ingredient',
    r'\bartifical\b':            'artificial',
    r'\bnatrual\b':              'natural',
    r'\bexellent\b':             'excellent',
    r'\bnutrional\b':            'nutritional',
}


def _check_spelling(ocr_text: str, fname: str) -> dict:
    issues, notes = [], []

    for pattern, correction in _MISSPELLINGS.items():
        if re.search(pattern, ocr_text, re.IGNORECASE):
            issues.append({
                'severity': 'warning',
                'message': (
                    f'Possible misspelling detected → should be "{correction}". '
                    'OCR on outlined-text PDFs can produce false positives; verify on the actual artwork file.'
                ),
            })

    if re.search(r'\bflavour\b', ocr_text, re.IGNORECASE):
        notes.append('UK spelling "flavour" detected. US market labels should use "flavor".')

    notes.append(
        'ProDough® wordmark check: verify capital P, capital D, no space, and the ® symbol appear correctly. '
        'Social handles (@prodoughshop) and website URLs (prodoughshop.com) are excluded from this check.'
    )

    return {'issues': issues, 'notes': notes}


# ── Check 5: FDA compliance ───────────────────────────────────────────────────

_DISEASE_CLAIM_PATTERNS = [
    (r'\btreat[s]?\b.{0,40}(disease|disorder|condition|syndrome)',
     'disease treatment claim — prohibited on food/supplement labels without FDA approval'),
    (r'\bcure[s]?\b.{0,40}(disease|disorder|cancer|diabetes)',
     'disease cure claim — prohibited without FDA approval'),
    (r'\bprevent[s]?\b.{0,40}(disease|cancer|diabetes|heart disease|stroke)',
     'disease prevention claim — prohibited without FDA approval (or an approved health claim)'),
    (r'lower[s]?\s+(your\s+)?cholesterol\b',
     '"lowers cholesterol" — authorized health claim requiring specific FDA-approved language (21 CFR 101.75)'),
    (r'reduc[es]+\s+.{0,20}risk\s+of\s+(cancer|diabetes|heart|stroke)',
     'disease risk-reduction claim — requires an FDA-approved health claim (21 CFR 101.14)'),
    (r'\bdiagnose[s]?\b.{0,30}(disease|disorder)',
     'diagnostic claim — prohibited on food/supplement labels'),
]

_SF_CLAIM_PATTERNS = [
    r'supports?\s+(muscle|digestion|immunity|gut\s+health|joint|bone|brain|cognitive|energy|weight\s+management)',
    r'promotes?\s+(muscle|recovery|digestion|immunity|gut|joint|bone|brain|lean\s+muscle)',
    r'helps?\s+maintain\s+(muscle|energy|immunity|weight|gut)',
    r'improves?\s+(recovery|performance|endurance|strength|focus)',
    r'boosts?\s+(metabolism|energy|immunity|performance|focus)',
    r'enhances?\s+(performance|recovery|endurance)',
]


def _ocr_is_sparse(ocr_text: str) -> bool:
    words = [w for w in ocr_text.split() if len(w) > 2]
    return len(words) < 80


def _check_fda(ocr_text: str, fname: str) -> dict:
    issues, notes = [], []
    tl = ocr_text.lower()

    is_supplement = 'supplement facts' in tl
    sparse = _ocr_is_sparse(ocr_text)

    if sparse:
        notes.append(
            'Low OCR yield — this PDF likely uses text converted to outlines (standard for print-ready files). '
            'The absence-based checks below (NFP, ingredients, allergens, manufacturer info) cannot be reliably '
            'automated on outlined-text PDFs. Use the Manual Review page to zoom in and verify each element '
            'directly on the rendered artwork image.'
        )

    # ── Required label elements (absence-based — unreliable on outlined PDFs) ─
    # Only flag these if OCR has meaningful yield; otherwise they are noise.

    if not sparse:
        if 'nutrition facts' not in tl and 'supplement facts' not in tl:
            issues.append({
                'severity': 'warning',
                'message': (
                    'Nutrition Facts (or Supplement Facts) panel not detected via OCR. '
                    'Required on all packaged food and dietary supplement labels (21 CFR 101.9 / 101.36). '
                    'Verify manually on the actual artwork.'
                ),
            })

        if 'ingredient' not in tl:
            issues.append({
                'severity': 'warning',
                'message': (
                    'Ingredient list not detected via OCR. '
                    'Required on virtually all packaged food labels (21 CFR 101.4). '
                    'Verify manually on the actual artwork.'
                ),
            })
    else:
        # Collapsed note for sparse OCR — avoid flooding the report with absence warnings
        notes.append(
            'Required label elements (NFP, ingredients, allergens, manufacturer info, net weight) '
            'could not be verified via OCR due to outlined text. '
            'Verify each of these is present and correctly formatted directly on the artwork file.'
        )

    # ── Allergen declaration ───────────────────────────────────────────────────
    has_allergen_stmt = any(re.search(p, tl) for p in
                            [r'contains?:', r'\ballergen\b', r'allergy\s+info', r'may\s+contain'])

    is_whey_product   = any(w in fname.lower() or w in tl for w in ['whey', 'protein stick', 'protein powder'])
    is_wheat_product  = any(w in fname.lower() or w in tl for w in ['pancake', 'donut', 'flour', 'mix'])

    if is_whey_product and 'milk' not in tl:
        issues.append({
            'severity': 'warning',
            'message': (
                'Whey protein product — "milk" allergen declaration not detected via OCR. '
                'FDA FALCPA (21 USC 343(w)) requires milk to be declared as a major food allergen. '
                'Verify the allergen statement is present on the actual artwork.'
            ),
        })
    elif is_wheat_product and 'wheat' not in tl:
        issues.append({
            'severity': 'warning',
            'message': (
                'Wheat-containing product type — "wheat" allergen not detected via OCR. '
                'FALCPA requires wheat to be declared as a major food allergen (21 USC 343(w)). '
                'Verify the allergen statement appears on the actual artwork.'
            ),
        })
    elif not has_allergen_stmt and not sparse:
        issues.append({
            'severity': 'warning',
            'message': (
                'No allergen declaration (e.g., "Contains: Milk") detected via OCR. '
                'FALCPA requires declaration of the 9 major allergens. Verify manually.'
            ),
        })

    # ── Manufacturer / distributor info ───────────────────────────────────────
    if not sparse:
        _mfr_indicators = [
            'manufactured by', 'distributed by', 'produced by', 'manufactured for',
            'distributed for', 'packed by', 'bottled by',
            'llc', 'inc.', 'corp.', 'company', 'co.', 'group', 'enterprises',
            'nutrition', 'foods', 'labs', 'ops', 'industries', 'international',
        ]
        has_mfr_text = any(ind in tl for ind in _mfr_indicators)
        has_zip      = bool(re.search(r'\b\d{5}\b', ocr_text))
        has_street   = bool(re.search(
            r'\b\d+\s+[NSEW]\.?\s+\d+|\b\d+\s+\w+\s+(st|ave|blvd|dr|rd|ln|way|pkwy|court|ct)\b', tl
        ))

        if not (has_mfr_text or has_zip or has_street):
            issues.append({
                'severity': 'warning',
                'message': (
                    'Manufacturer or distributor name/address not detected via OCR. '
                    '21 CFR 101.5 requires the name and place of business of the manufacturer, packer, '
                    'or distributor. Verify it appears on the actual artwork.'
                ),
            })

    # ── Net weight ────────────────────────────────────────────────────────────
    if not sparse and not re.search(r'net\s*wt|net\s*weight', tl):
        issues.append({
            'severity': 'warning',
            'message': (
                'No "Net Wt" declaration detected. '
                'Net quantity of contents is required (15 USC 1453 / 21 CFR 101.105). Verify manually.'
            ),
        })
    elif re.search(r'net\s*wt|net\s*weight', tl):
        has_g  = bool(re.search(r'net\s*wt.{0,20}\d+\s*g\b', tl))
        has_oz = bool(re.search(r'\d+\.?\d*\s*oz\b', tl))
        if has_g and not has_oz:
            notes.append(
                'Net weight appears in grams only. '
                'US regulations generally require both metric (g) and US customary (oz) units. '
                'Verify with your legal/regulatory team.'
            )

    # ── % Daily Values footnote ───────────────────────────────────────────────
    if re.search(r'%\s*daily\s*value', tl) and 'based on a 2,000 calorie' not in tl and '2,000 calorie diet' not in tl:
        notes.append(
            '% Daily Values listed but the required footnote — '
            '"Percent Daily Values are based on a 2,000 calorie diet" — '
            'was not detected (21 CFR 101.9(d)(9)).'
        )

    # ── Disease claims ────────────────────────────────────────────────────────
    for pattern, description in _DISEASE_CLAIM_PATTERNS:
        if re.search(pattern, tl):
            issues.append({
                'severity': 'critical',
                'message': (
                    f'Potential unauthorized disease claim detected: {description}. '
                    'Unapproved disease claims are prohibited (21 CFR 101.14 / FD&C Act 403(r)).'
                ),
            })

    # ── Structure/function claims + disclaimer ────────────────────────────────
    sf_found = [p for p in _SF_CLAIM_PATTERNS if re.search(p, tl)]
    if sf_found and is_supplement:
        has_disclaimer = any(phrase in tl for phrase in [
            'not been evaluated by the food and drug',
            'not been evaluated by the fda',
            'not intended to diagnose',
            'these statements have not',
        ])
        if not has_disclaimer:
            issues.append({
                'severity': 'critical',
                'message': (
                    'Structure/function claims detected on a Supplement Facts product '
                    'but the required FDA disclaimer is missing. '
                    'Per 21 CFR 101.93, labels must include: '
                    '"These statements have not been evaluated by the Food and Drug Administration. '
                    'This product is not intended to diagnose, treat, cure, or prevent any disease."'
                ),
            })
    elif sf_found and not is_supplement:
        notes.append(
            'Structure/function-style claims ("supports...", "promotes...", etc.) on a conventional food label. '
            'Generally permissible but must not imply treatment or prevention of disease.'
        )

    # ── "All Natural" / "Natural" claim ──────────────────────────────────────
    if re.search(r'\ball\s+natural\b|\bnatural\s+ingredients?\b|\b100%\s+natural\b', tl):
        artificial_markers = [
            'artificial', 'synthetic', 'fd&c', 'fdc', 'acesulfame', 'sucralose',
            'aspartame', 'sodium benzoate', 'bht', 'bha', 'tbhq', 'carrageenan',
        ]
        if any(m in tl for m in artificial_markers):
            issues.append({
                'severity': 'critical',
                'message': (
                    '"Natural" or "All Natural" claim appears alongside what may be an artificial ingredient. '
                    'FDA\'s informal policy considers "natural" to mean nothing artificial or synthetic has been added.'
                ),
            })
        else:
            notes.append(
                '"All Natural" or "Natural Ingredients" claim detected. '
                'Ensure no artificial flavors, colors, or synthetic preservatives are present (FDA 2018 guidance).'
            )

    # ── Gluten-Free claim ─────────────────────────────────────────────────────
    if re.search(r'gluten[\s\-]?free', tl):
        if any(w in tl for w in ['wheat', 'barley', 'rye', 'triticale']):
            issues.append({
                'severity': 'critical',
                'message': (
                    '"Gluten-Free" claim detected alongside wheat/barley/rye in OCR text. '
                    'If the product contains these ingredients, the GF claim is prohibited (21 CFR 101.91).'
                ),
            })
        else:
            notes.append(
                '"Gluten-Free" claim detected. Under 21 CFR 101.91, product must contain < 20 ppm gluten. '
                'Ensure third-party testing records are on file.'
            )

    # ── Organic claim ─────────────────────────────────────────────────────────
    if re.search(r'\borganic\b', tl):
        if 'usda organic' not in tl and 'certified organic' not in tl:
            issues.append({
                'severity': 'warning',
                'message': (
                    '"Organic" claim detected without apparent USDA Organic seal or "Certified Organic" language. '
                    'Organic claims require USDA NOP certification (7 CFR Part 205).'
                ),
            })

    # ── Nutrient content claims ───────────────────────────────────────────────
    if re.search(r'high\s+protein|excellent\s+source\s+of\s+protein', tl):
        notes.append(
            '"High Protein" / "Excellent Source of Protein" claim: '
            'FDA requires ≥ 10g protein per RACC and ≥ 20% DV corrected for PDCAAS (21 CFR 101.54(e)).'
        )

    if re.search(r'good\s+source\s+of\s+protein', tl):
        notes.append('"Good Source of Protein" claim: FDA requires 10–19% DV (21 CFR 101.54(e)).')

    # ── Grass-Fed claim ───────────────────────────────────────────────────────
    if re.search(r'grass[\s\-]?fed', tl):
        notes.append(
            '"Grass-Fed" claim detected. Ensure your whey protein supplier can provide documentation. '
            'Third-party certification (e.g., AGA) is strongly recommended.'
        )

    # ── 100% protein source claim ─────────────────────────────────────────────
    if re.search(r'100%\s+(whey|plant|beef)\s+protein', tl):
        notes.append(
            '"100% [Whey/Plant/Beef] Protein" claim: verify there are no other protein sources in the formulation.'
        )

    # ── Sesame allergen (FASTER Act, Jan 2023) ────────────────────────────────
    if re.search(r'\bsesame\b|\btahini\b', tl):
        notes.append(
            'Sesame detected. Under the FASTER Act (effective Jan 1, 2023), sesame is the 9th major allergen '
            'and must be declared on US food labels (21 USC 321(qq)(2)).'
        )

    # ── Bioengineered food disclosure ─────────────────────────────────────────
    if any(re.search(p, tl) for p in [r'non[\s\-]?gmo', r'bioengineered', r'derived from bioengineering']):
        notes.append(
            'Non-GMO or Bioengineered (BE) disclosure detected. '
            'USDA\'s BE Food Disclosure Standard (7 CFR Part 66, effective Jan 2022) requires specific language. '
            '"Non-GMO" is not a compliant substitute for BE disclosure if the product is in fact BE.'
        )

    # ── "Made in USA" claim ───────────────────────────────────────────────────
    if re.search(r'made\s+in\s+(the\s+)?usa|made\s+in\s+america', tl):
        notes.append(
            '"Made in USA" claim detected. FTC requires "all or virtually all" US-origin content. '
            'If foreign-sourced ingredients are used, the claim may need to be qualified.'
        )

    # ── Caffeine / stimulants ─────────────────────────────────────────────────
    if re.search(r'\bcaffeine\b|\bguarana\b|\bgreen\s+tea\s+extract\b|\bgreen\s+coffee\b', tl):
        notes.append(
            'Caffeine or stimulant ingredient detected. '
            'Ensure caffeine amount is listed and total is within safe limits.'
        )

    return {'issues': issues, 'notes': notes}


# ── Summary ───────────────────────────────────────────────────────────────────

def _build_summary(results: list) -> dict:
    total = len(results)
    counts = {'clean': 0, 'info': 0, 'warning': 0, 'critical': 0, 'error': 0}
    for r in results:
        sev = r.get('severity', 'error')
        counts[sev] = counts.get(sev, 0) + 1

    total_crits = sum(r.get('critical_count', 0) for r in results)
    total_warns = sum(r.get('warning_count', 0) for r in results)

    fda_crits = []
    for r in results:
        for issue in r.get('checks', {}).get('fda', {}).get('issues', []):
            if issue['severity'] == 'critical':
                fda_crits.append({'file': r['filename'], 'message': issue['message']})

    return {
        'total_files': total,
        'severity_counts': counts,
        'total_critical_issues': total_crits,
        'total_warning_issues': total_warns,
        'fda_critical_issues': fda_crits,
    }

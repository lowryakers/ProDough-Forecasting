import os
import json
import sqlite3
import io
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, g
import openpyxl
import pandas as pd

app = Flask(__name__)
app.secret_key = 'prodough-forecasting-2024'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'instance', 'forecasting.db')
DATA_DIR = os.path.join(BASE_DIR, 'data')
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

SUPER_SACK_GRAMS = 400_000

# Reorder points by tier (days)
TIER_REORDER = {1: 60, 2: 100, 3: 130}
TARGET_DAYS = 240


# ── Data helpers ─────────────────────────────────────────────────────────────

def load_skus():
    with open(os.path.join(DATA_DIR, 'skus.json')) as f:
        return json.load(f)


def load_mappings():
    with open(os.path.join(DATA_DIR, 'sku_mappings.json')) as f:
        data = json.load(f)
    merchant_to_prodough = {m['merchant_sku']: m['prodough_sku'] for m in data if m['merchant_sku']}
    asin_to_prodough = {m['asin']: m['prodough_sku'] for m in data if m['asin']}
    return merchant_to_prodough, asin_to_prodough, data


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS inventory_import (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_date TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'shiphero',
            filename TEXT,
            row_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS inventory_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            product_name TEXT,
            on_hand INTEGER DEFAULT 0,
            available INTEGER DEFAULT 0,
            warehouse TEXT,
            FOREIGN KEY (import_id) REFERENCES inventory_import(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS sales_import (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_date TEXT NOT NULL,
            source TEXT NOT NULL,
            period_days INTEGER NOT NULL,
            filename TEXT,
            row_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sales_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            product_name TEXT,
            units_sold INTEGER DEFAULT 0,
            FOREIGN KEY (import_id) REFERENCES sales_import(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS sku_tier (
            sku TEXT PRIMARY KEY,
            tier INTEGER NOT NULL DEFAULT 2
        );
        CREATE TABLE IF NOT EXISTS forecast_session (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_date TEXT NOT NULL,
            po_number TEXT,
            notes TEXT,
            inventory_import_id INTEGER,
            shopify_import_id INTEGER,
            amazon_import_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS forecast_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            product_name TEXT,
            category TEXT,
            current_inventory INTEGER DEFAULT 0,
            avg_daily_demand REAL DEFAULT 0,
            days_of_demand REAL DEFAULT 0,
            reorder_point INTEGER DEFAULT 100,
            target_days INTEGER DEFAULT 240,
            needs_reorder INTEGER DEFAULT 0,
            suggested_qty INTEGER DEFAULT 0,
            final_qty INTEGER DEFAULT 0,
            super_sacks REAL DEFAULT 0,
            FOREIGN KEY (forecast_id) REFERENCES forecast_session(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS api_credentials (
            service TEXT PRIMARY KEY,
            credentials TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_date TEXT NOT NULL,
            service TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            records_synced INTEGER DEFAULT 0
        );
    ''')
    # Migrate: add source column if missing (for existing databases)
    try:
        db.execute("ALTER TABLE inventory_import ADD COLUMN source TEXT NOT NULL DEFAULT 'shiphero'")
        db.commit()
    except Exception:
        pass
    db.commit()
    db.close()


# ── File parsers ──────────────────────────────────────────────────────────────

def parse_shiphero(file_data, filename):
    """Parse ShipHero inventory export. Returns list of {sku, product_name, on_hand, available, warehouse}."""
    merchant_to_prodough, _, _ = load_mappings()
    skus_by_sku = {s['sku']: s for s in load_skus()}
    items = []

    try:
        if filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(file_data))
        else:
            df = pd.read_excel(io.BytesIO(file_data))
    except Exception as e:
        raise ValueError(f'Could not read file: {e}')

    df.columns = [str(c).strip().lower().replace(' ', '_') for c in df.columns]

    # Find relevant columns
    name_col = next((c for c in df.columns if 'name' in c or 'product' in c), None)
    sku_col = next((c for c in df.columns if c == 'sku'), None)
    on_hand_col = next((c for c in df.columns if 'on_hand' in c or 'onhand' in c), None)
    avail_col = next((c for c in df.columns if 'available' in c or 'avail' in c), None)
    wh_col = next((c for c in df.columns if 'warehouse' in c), None)

    if not sku_col or not on_hand_col:
        raise ValueError('Could not find SKU or On Hand columns in ShipHero export')

    seen = {}
    for _, row in df.iterrows():
        raw_sku = str(row.get(sku_col, '')).strip()
        if not raw_sku or raw_sku.lower() in ('nan', 'sku', 'none', ''):
            continue

        # Map merchant SKU → ProDough SKU
        prodough_sku = merchant_to_prodough.get(raw_sku, raw_sku)

        # Accept if it matches a known ProDough SKU
        if prodough_sku not in skus_by_sku:
            # Try case-insensitive
            match = next((k for k in skus_by_sku if k.upper() == prodough_sku.upper()), None)
            if match:
                prodough_sku = match
            else:
                continue  # skip unknown SKUs

        on_hand = int(float(str(row.get(on_hand_col, 0) or 0).replace(',', '')))
        available = int(float(str(row.get(avail_col, on_hand) or 0).replace(',', ''))) if avail_col else on_hand
        name = str(row.get(name_col, '')) if name_col else ''
        warehouse = str(row.get(wh_col, '')) if wh_col else ''

        if prodough_sku in seen:
            seen[prodough_sku]['on_hand'] += on_hand
            seen[prodough_sku]['available'] += available
        else:
            seen[prodough_sku] = {
                'sku': prodough_sku,
                'product_name': skus_by_sku[prodough_sku]['display_name'] or name,
                'on_hand': on_hand,
                'available': available,
                'warehouse': warehouse,
            }

    return list(seen.values())


def parse_shopify(file_data, filename, period_days):
    """Parse Shopify sales export. Returns list of {sku, product_name, units_sold}."""
    skus_by_sku = {s['sku']: s for s in load_skus()}
    items = {}

    try:
        if filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(file_data))
        else:
            df = pd.read_excel(io.BytesIO(file_data))
    except Exception as e:
        raise ValueError(f'Could not read file: {e}')

    df.columns = [str(c).strip().lower().replace(' ', '_') for c in df.columns]

    # Shopify exports two column name styles
    sku_col = next((c for c in df.columns if 'sku' in c and 'variant' in c), None) or \
              next((c for c in df.columns if c == 'variant_sku'), None)
    qty_col = next((c for c in df.columns if 'net_item' in c or 'net_quantity' in c or 'quantity' in c), None)
    name_col = next((c for c in df.columns if 'product' in c and 'title' in c), None)

    if not sku_col or not qty_col:
        raise ValueError('Could not find SKU or quantity columns in Shopify export')

    for _, row in df.iterrows():
        sku = str(row.get(sku_col, '')).strip()
        if not sku or sku.lower() in ('nan', 'none', ''):
            continue
        if sku not in skus_by_sku:
            match = next((k for k in skus_by_sku if k.upper() == sku.upper()), None)
            if not match:
                continue
            sku = match

        qty = int(float(str(row.get(qty_col, 0) or 0).replace(',', '')))
        if qty <= 0:
            continue
        name = str(row.get(name_col, '')) if name_col else ''

        if sku in items:
            items[sku]['units_sold'] += qty
        else:
            items[sku] = {
                'sku': sku,
                'product_name': skus_by_sku[sku]['display_name'] or name,
                'units_sold': qty,
            }

    return list(items.values())


def parse_amazon(file_data, filename, period_days):
    """Parse Amazon Business Report. Returns list of {sku, product_name, units_sold}."""
    _, asin_to_prodough, _ = load_mappings()
    skus_by_sku = {s['sku']: s for s in load_skus()}
    items = {}

    try:
        if filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(file_data))
        else:
            df = pd.read_excel(io.BytesIO(file_data))
    except Exception as e:
        raise ValueError(f'Could not read file: {e}')

    df.columns = [str(c).strip().lower().replace(' ', '_').replace('(', '').replace(')', '').replace(' ', '_') for c in df.columns]

    # Amazon report columns: (Child) ASIN → Units Ordered
    asin_col = next((c for c in df.columns if 'child' in c and 'asin' in c), None) or \
               next((c for c in df.columns if 'asin' in c), None)
    units_col = next((c for c in df.columns if 'units_ordered' in c and 'b2b' not in c), None)
    title_col = next((c for c in df.columns if 'title' in c), None)

    if not asin_col or not units_col:
        raise ValueError('Could not find ASIN or Units Ordered columns in Amazon report')

    for _, row in df.iterrows():
        asin = str(row.get(asin_col, '')).strip()
        if not asin or asin.lower() in ('nan', 'none', ''):
            continue

        prodough_sku = asin_to_prodough.get(asin)
        if not prodough_sku:
            continue

        units = int(float(str(row.get(units_col, 0) or 0).replace(',', '')))
        if units <= 0:
            continue
        name = str(row.get(title_col, '')) if title_col else ''

        if prodough_sku in items:
            items[prodough_sku]['units_sold'] += units
        else:
            items[prodough_sku] = {
                'sku': prodough_sku,
                'product_name': skus_by_sku.get(prodough_sku, {}).get('display_name', '') or name,
                'units_sold': units,
            }

    return list(items.values())


def parse_amazon_fba_inventory(file_data, filename):
    """Parse Amazon FBA Restock Report (Inventory > FBA Inventory > Reports > Re-stock Report).
    Returns list of {sku, product_name, on_hand, available}.
    Handles both TSV (tab-separated) and CSV formats."""
    _, asin_to_prodough, _ = load_mappings()
    skus_by_sku = {s['sku']: s for s in load_skus()}
    items = {}

    try:
        if filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(file_data))
        elif filename.endswith('.txt') or filename.endswith('.tsv'):
            df = pd.read_csv(io.BytesIO(file_data), sep='\t')
        else:
            # Try tab first, fall back to comma
            try:
                df = pd.read_csv(io.BytesIO(file_data), sep='\t')
                if len(df.columns) < 3:
                    df = pd.read_csv(io.BytesIO(file_data))
            except Exception:
                df = pd.read_excel(io.BytesIO(file_data))
    except Exception as e:
        raise ValueError(f'Could not read file: {e}')

    # Normalize column names
    df.columns = [
        str(c).strip().lower()
          .replace(' ', '_').replace('-', '_')
          .replace('(', '').replace(')', '')
        for c in df.columns
    ]

    # Find ASIN/SKU columns — Amazon restock report uses "asin" and "sku" or "merchant_sku"
    asin_col = next((c for c in df.columns if c == 'asin'), None)
    sku_col = next((c for c in df.columns if c in ('sku', 'merchant_sku', 'merchant-sku', 'seller_sku')), None)
    name_col = next((c for c in df.columns if 'product_name' in c or 'title' in c or 'name' in c), None)

    # Quantity columns — prefer afn (Amazon FBA) over mfn (merchant-fulfilled)
    # Amazon restock report uses: afn-fulfillable-quantity, afn-total-quantity
    qty_col = next((c for c in df.columns if c.startswith('afn') and 'fulfillable' in c), None) or \
              next((c for c in df.columns if 'fulfillable' in c), None) or \
              next((c for c in df.columns if c == 'available'), None) or \
              next((c for c in df.columns if 'qty' in c and 'inbound' not in c and 'mfn' not in c), None)
    total_col = next((c for c in df.columns if c.startswith('afn') and 'total' in c), None) or \
                next((c for c in df.columns if 'total' in c and 'qty' in c), None) or qty_col

    if not (asin_col or sku_col):
        raise ValueError('Could not find ASIN or SKU column in Amazon inventory report. '
                         'Expected columns: asin, sku, merchant-sku')
    if not qty_col:
        raise ValueError('Could not find quantity column. Expected: afn-fulfillable-quantity, available, or similar')

    merchant_to_prodough, _, _ = load_mappings()

    for _, row in df.iterrows():
        prodough_sku = None

        # Try ASIN mapping first
        if asin_col:
            asin = str(row.get(asin_col, '')).strip()
            if asin and asin.lower() not in ('nan', 'none', ''):
                prodough_sku = asin_to_prodough.get(asin)

        # Fall back to merchant SKU mapping
        if not prodough_sku and sku_col:
            raw_sku = str(row.get(sku_col, '')).strip()
            if raw_sku and raw_sku.lower() not in ('nan', 'none', ''):
                prodough_sku = merchant_to_prodough.get(raw_sku, raw_sku)
                if prodough_sku not in skus_by_sku:
                    prodough_sku = None

        if not prodough_sku:
            continue

        available = int(float(row.get(qty_col, 0) or 0))
        on_hand = int(float(row.get(total_col, available) or 0)) if total_col != qty_col else available
        name = str(row.get(name_col, '')) if name_col else ''

        if prodough_sku in items:
            items[prodough_sku]['on_hand'] += on_hand
            items[prodough_sku]['available'] += available
        else:
            items[prodough_sku] = {
                'sku': prodough_sku,
                'product_name': skus_by_sku.get(prodough_sku, {}).get('display_name', '') or name,
                'on_hand': on_hand,
                'available': available,
                'warehouse': 'Amazon FBA',
            }

    return list(items.values())


# ── Forecast logic ─────────────────────────────────────────────────────────────

def compute_forecast(inventory_import_id, shopify_import_id, amazon_import_ids, tier_overrides=None):
    """Build forecast rows for all SKUs given import IDs.
    amazon_import_ids can be a single int, a list of ints, or None.
    When multiple Amazon imports are provided (15d/30d/60d), demand is computed
    as a weighted average: 15d=50%, 30d=35%, 60d=15%.
    """
    db = get_db()
    all_skus = load_skus()

    # Normalise amazon_import_ids to a list
    if amazon_import_ids is None:
        amazon_import_ids = []
    elif isinstance(amazon_import_ids, int):
        amazon_import_ids = [amazon_import_ids]

    # Load custom tier overrides from DB
    tier_rows = db.execute('SELECT sku, tier FROM sku_tier').fetchall()
    tier_db = {r['sku']: r['tier'] for r in tier_rows}

    # Load inventory — merge ShipHero + Amazon FBA if both present
    inv = {}
    if inventory_import_id:
        if not isinstance(inventory_import_id, list):
            inventory_import_id = [inventory_import_id]
        for imp_id in inventory_import_id:
            rows = db.execute('SELECT sku, on_hand FROM inventory_item WHERE import_id=?', (imp_id,)).fetchall()
            for r in rows:
                inv[r['sku']] = inv.get(r['sku'], 0) + r['on_hand']

    # Load Shopify sales
    shopify_sales = {}
    shopify_days = 30
    if shopify_import_id:
        imp = db.execute('SELECT period_days FROM sales_import WHERE id=?', (shopify_import_id,)).fetchone()
        if imp:
            shopify_days = imp['period_days']
        rows = db.execute('SELECT sku, units_sold FROM sales_item WHERE import_id=?', (shopify_import_id,)).fetchall()
        for r in rows:
            shopify_sales[r['sku']] = r['units_sold']

    # Load Amazon sales — weighted average across all provided periods
    # Weights by period: shortest period = highest weight (most current signal)
    PERIOD_WEIGHTS = {15: 0.50, 30: 0.35, 60: 0.15}

    amazon_period_data = []  # list of (days, {sku: units})
    for az_id in amazon_import_ids:
        imp = db.execute('SELECT period_days FROM sales_import WHERE id=?', (az_id,)).fetchone()
        if not imp:
            continue
        days = imp['period_days']
        rows = db.execute('SELECT sku, units_sold FROM sales_item WHERE import_id=?', (az_id,)).fetchall()
        sales = {r['sku']: r['units_sold'] for r in rows}
        amazon_period_data.append((days, sales))

    # Build weighted Amazon daily demand per SKU
    def weighted_az_daily(sku):
        if not amazon_period_data:
            return 0.0
        if len(amazon_period_data) == 1:
            days, sales = amazon_period_data[0]
            return sales.get(sku, 0) / days if days else 0.0
        # Multi-period weighted average
        total_weight = 0.0
        weighted_sum = 0.0
        for days, sales in amazon_period_data:
            w = PERIOD_WEIGHTS.get(days, 1 / days)
            daily = sales.get(sku, 0) / days if days else 0.0
            weighted_sum += daily * w
            total_weight += w
        return weighted_sum / total_weight if total_weight else 0.0

    results = []
    for s in all_skus:
        sku = s['sku']
        current_inv = inv.get(sku, 0)

        sh_daily = (shopify_sales.get(sku, 0) / shopify_days) if shopify_days else 0
        az_daily = weighted_az_daily(sku)
        avg_daily = sh_daily + az_daily

        # Days of demand
        days_of_demand = (current_inv / avg_daily) if avg_daily > 0 else (9999 if current_inv > 0 else 0)

        # Tier & reorder point
        tier = tier_overrides.get(sku) if tier_overrides else None
        if tier is None:
            tier = tier_db.get(sku, s.get('tier', 2))
        reorder_point = TIER_REORDER.get(tier, 100)

        needs_reorder = days_of_demand < reorder_point if avg_daily > 0 else False

        # How many units to bring us to TARGET_DAYS
        if avg_daily > 0:
            units_needed = max(0, (TARGET_DAYS - days_of_demand) * avg_daily)
        else:
            units_needed = 0

        # Round up to MOQ
        moq = s['moq']
        if units_needed > 0:
            suggested_qty = moq * max(1, -(-int(units_needed) // moq))  # ceiling division
        else:
            suggested_qty = 0

        # Super sacks
        weight_g = s['weight_grams']
        super_sacks = (suggested_qty * weight_g / SUPER_SACK_GRAMS) if suggested_qty > 0 else 0

        # Amazon period breakdown for display
        az_periods = {}
        for days, sales in amazon_period_data:
            az_periods[f'az_{days}d'] = sales.get(sku, 0)

        results.append({
            'sku': sku,
            'product_name': s['display_name'],
            'category': s['category'],
            'prefix': s['prefix'],
            'moq': moq,
            'weight_grams': weight_g,
            'tier': tier,
            'reorder_point': reorder_point,
            'current_inventory': current_inv,
            'shopify_units': shopify_sales.get(sku, 0),
            'amazon_periods': az_periods,
            'shopify_daily': round(sh_daily, 2),
            'amazon_daily': round(az_daily, 2),
            'avg_daily_demand': round(avg_daily, 2),
            'days_of_demand': round(days_of_demand, 1) if days_of_demand < 9999 else None,
            'target_days': TARGET_DAYS,
            'needs_reorder': needs_reorder,
            'units_needed': round(units_needed),
            'suggested_qty': suggested_qty,
            'final_qty': suggested_qty,
            'super_sacks': round(super_sacks, 2),
        })

    return results


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    db = get_db()
    inv_imports = db.execute('SELECT * FROM inventory_import ORDER BY import_date DESC LIMIT 5').fetchall()
    sales_imports = db.execute('SELECT * FROM sales_import ORDER BY import_date DESC LIMIT 10').fetchall()
    forecasts = db.execute('SELECT * FROM forecast_session ORDER BY created_date DESC LIMIT 5').fetchall()

    latest_inv = db.execute('SELECT * FROM inventory_import ORDER BY import_date DESC LIMIT 1').fetchone()
    latest_shopify = db.execute("SELECT * FROM sales_import WHERE source='shopify' ORDER BY import_date DESC LIMIT 1").fetchone()
    latest_amazon = db.execute("SELECT * FROM sales_import WHERE source='amazon' ORDER BY import_date DESC LIMIT 1").fetchone()

    stats = {}
    if latest_inv:
        total_units = db.execute('SELECT SUM(on_hand) FROM inventory_item WHERE import_id=?', (latest_inv['id'],)).fetchone()[0] or 0
        sku_count = db.execute('SELECT COUNT(*) FROM inventory_item WHERE import_id=?', (latest_inv['id'],)).fetchone()[0] or 0
        stats['total_units'] = total_units
        stats['sku_count'] = sku_count

    return render_template('index.html',
        inv_imports=inv_imports,
        sales_imports=sales_imports,
        forecasts=forecasts,
        latest_inv=latest_inv,
        latest_shopify=latest_shopify,
        latest_amazon=latest_amazon,
        stats=stats,
        all_sku_count=117,
    )


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        source = request.form.get('source')
        db = get_db()

        try:
            if source == 'shiphero':
                file = request.files.get('file')
                if not file or not file.filename:
                    flash('No file selected.', 'danger')
                    return redirect(url_for('upload'))
                items = parse_shiphero(file.read(), file.filename)
                imp = db.execute(
                    "INSERT INTO inventory_import (import_date, source, filename, row_count) VALUES (?,?,?,?)",
                    (datetime.now().isoformat(), 'shiphero', file.filename, len(items))
                )
                imp_id = imp.lastrowid
                for it in items:
                    db.execute(
                        'INSERT INTO inventory_item (import_id, sku, product_name, on_hand, available, warehouse) VALUES (?,?,?,?,?,?)',
                        (imp_id, it['sku'], it['product_name'], it['on_hand'], it['available'], it['warehouse'])
                    )
                db.commit()
                flash(f'ShipHero inventory imported: {len(items)} SKUs matched.', 'success')

            elif source == 'amazon_inventory':
                file = request.files.get('file')
                if not file or not file.filename:
                    flash('No file selected.', 'danger')
                    return redirect(url_for('upload'))
                items = parse_amazon_fba_inventory(file.read(), file.filename)
                imp = db.execute(
                    "INSERT INTO inventory_import (import_date, source, filename, row_count) VALUES (?,?,?,?)",
                    (datetime.now().isoformat(), 'amazon_fba', file.filename, len(items))
                )
                imp_id = imp.lastrowid
                for it in items:
                    db.execute(
                        'INSERT INTO inventory_item (import_id, sku, product_name, on_hand, available, warehouse) VALUES (?,?,?,?,?,?)',
                        (imp_id, it['sku'], it['product_name'], it['on_hand'], it['available'], it['warehouse'])
                    )
                db.commit()
                flash(f'Amazon FBA inventory imported: {len(items)} SKUs matched.', 'success')

            elif source == 'shopify':
                file = request.files.get('file')
                period_days = int(request.form.get('period_days', 30))
                if not file or not file.filename:
                    flash('No file selected.', 'danger')
                    return redirect(url_for('upload'))
                items = parse_shopify(file.read(), file.filename, period_days)
                imp = db.execute(
                    'INSERT INTO sales_import (import_date, source, period_days, filename, row_count) VALUES (?,?,?,?,?)',
                    (datetime.now().isoformat(), 'shopify', period_days, file.filename, len(items))
                )
                imp_id = imp.lastrowid
                for it in items:
                    db.execute('INSERT INTO sales_item (import_id, sku, product_name, units_sold) VALUES (?,?,?,?)',
                               (imp_id, it['sku'], it['product_name'], it['units_sold']))
                db.commit()
                flash(f'Shopify sales imported: {len(items)} SKUs matched over {period_days} days.', 'success')

            elif source == 'amazon_sales':
                # Multi-file: up to three period files uploaded at once
                period_configs = [
                    ('file_15d', 15),
                    ('file_30d', 30),
                    ('file_60d', 60),
                ]
                imported_count = 0
                for field_name, days in period_configs:
                    file = request.files.get(field_name)
                    if not file or not file.filename:
                        continue
                    items = parse_amazon(file.read(), file.filename, days)
                    if not items:
                        flash(f'Amazon {days}d report: no matching SKUs found in {file.filename}.', 'warning')
                        continue
                    imp = db.execute(
                        'INSERT INTO sales_import (import_date, source, period_days, filename, row_count) VALUES (?,?,?,?,?)',
                        (datetime.now().isoformat(), 'amazon', days, file.filename, len(items))
                    )
                    imp_id = imp.lastrowid
                    for it in items:
                        db.execute('INSERT INTO sales_item (import_id, sku, product_name, units_sold) VALUES (?,?,?,?)',
                                   (imp_id, it['sku'], it['product_name'], it['units_sold']))
                    imported_count += 1
                    flash(f'Amazon {days}d sales imported: {len(items)} SKUs matched.', 'success')
                db.commit()
                if imported_count == 0:
                    flash('No Amazon sales files were uploaded.', 'warning')
            else:
                flash('Unknown source type.', 'danger')

        except ValueError as e:
            db.rollback()
            flash(f'Import error: {e}', 'danger')

        return redirect(url_for('upload'))

    db = get_db()
    inv_imports = db.execute('SELECT * FROM inventory_import ORDER BY import_date DESC').fetchall()
    sales_imports = db.execute('SELECT * FROM sales_import ORDER BY import_date DESC').fetchall()
    return render_template('upload.html', inv_imports=inv_imports, sales_imports=sales_imports)


@app.route('/upload/delete/<string:type>/<int:import_id>', methods=['POST'])
def delete_import(type, import_id):
    db = get_db()
    if type == 'inventory':
        db.execute('DELETE FROM inventory_item WHERE import_id=?', (import_id,))
        db.execute('DELETE FROM inventory_import WHERE id=?', (import_id,))
    else:
        db.execute('DELETE FROM sales_item WHERE import_id=?', (import_id,))
        db.execute('DELETE FROM sales_import WHERE id=?', (import_id,))
    db.commit()
    flash('Import deleted.', 'info')
    return redirect(url_for('upload'))


@app.route('/inventory')
def inventory():
    db = get_db()
    imports = db.execute('SELECT * FROM inventory_import ORDER BY import_date DESC').fetchall()
    selected_id = request.args.get('import_id', type=int)
    if not selected_id and imports:
        selected_id = imports[0]['id']

    items = []
    selected_import = None
    if selected_id:
        selected_import = db.execute('SELECT * FROM inventory_import WHERE id=?', (selected_id,)).fetchone()
        rows = db.execute(
            'SELECT * FROM inventory_item WHERE import_id=? ORDER BY sku',
            (selected_id,)
        ).fetchall()
        all_skus = {s['sku']: s for s in load_skus()}
        for r in rows:
            s = all_skus.get(r['sku'], {})
            items.append({
                'sku': r['sku'],
                'product_name': r['product_name'] or s.get('display_name', ''),
                'category': s.get('category', 'Other'),
                'on_hand': r['on_hand'],
                'available': r['available'],
                'warehouse': r['warehouse'],
                'moq': s.get('moq', 1200),
            })
        items.sort(key=lambda x: (x['category'], x['sku']))

    return render_template('inventory.html',
        imports=imports,
        selected_id=selected_id,
        selected_import=selected_import,
        items=items,
    )


@app.route('/forecast', methods=['GET', 'POST'])
def forecast():
    db = get_db()

    inv_imports = db.execute('SELECT * FROM inventory_import ORDER BY import_date DESC').fetchall()
    shopify_imports = db.execute("SELECT * FROM sales_import WHERE source='shopify' ORDER BY import_date DESC").fetchall()
    amazon_imports = db.execute("SELECT * FROM sales_import WHERE source='amazon' ORDER BY import_date DESC").fetchall()

    if request.method == 'POST':
        # Inventory: may be multiple (ShipHero + Amazon FBA)
        inv_ids = request.form.getlist('inventory_import_id')
        inv_ids = [int(i) for i in inv_ids if i]
        inv_id = inv_ids if inv_ids else None

        sh_id = request.form.get('shopify_import_id', type=int)

        # Amazon sales: may be multiple periods
        az_ids = request.form.getlist('amazon_import_ids')
        az_ids = [int(i) for i in az_ids if i]

        po_number = request.form.get('po_number', '').strip()
        notes = request.form.get('notes', '').strip()

        if not inv_id and not sh_id and not az_ids:
            flash('Select at least one data source.', 'danger')
            return redirect(url_for('forecast'))

        rows = compute_forecast(inv_id, sh_id, az_ids)

        # Save forecast — store amazon IDs as comma-separated string
        fs = db.execute(
            'INSERT INTO forecast_session (created_date, po_number, notes, inventory_import_id, shopify_import_id, amazon_import_id) VALUES (?,?,?,?,?,?)',
            (datetime.now().isoformat(), po_number, notes,
             ','.join(str(i) for i in (inv_id or [])),
             sh_id,
             ','.join(str(i) for i in az_ids))
        )
        forecast_id = fs.lastrowid

        for r in rows:
            db.execute('''INSERT INTO forecast_item
                (forecast_id, sku, product_name, category, current_inventory, avg_daily_demand,
                 days_of_demand, reorder_point, target_days, needs_reorder, suggested_qty, final_qty, super_sacks)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (forecast_id, r['sku'], r['product_name'], r['category'],
                 r['current_inventory'], r['avg_daily_demand'],
                 r['days_of_demand'], r['reorder_point'], r['target_days'],
                 1 if r['needs_reorder'] else 0,
                 r['suggested_qty'], r['final_qty'], r['super_sacks'])
            )
        db.commit()
        flash(f'Forecast generated with {sum(1 for r in rows if r["suggested_qty"] > 0)} SKUs needing orders.', 'success')
        return redirect(url_for('view_forecast', forecast_id=forecast_id))

    latest_inv = inv_imports[0] if inv_imports else None
    latest_shopify = shopify_imports[0] if shopify_imports else None
    latest_amazon = amazon_imports[0] if amazon_imports else None

    return render_template('forecast.html',
        inv_imports=inv_imports,
        shopify_imports=shopify_imports,
        amazon_imports=amazon_imports,
        latest_inv=latest_inv,
        latest_shopify=latest_shopify,
        latest_amazon=latest_amazon,
        tier_reorder=TIER_REORDER,
        target_days=TARGET_DAYS,
    )


@app.route('/forecast/<int:forecast_id>')
def view_forecast(forecast_id):
    db = get_db()
    session = db.execute('SELECT * FROM forecast_session WHERE id=?', (forecast_id,)).fetchone()
    if not session:
        flash('Forecast not found.', 'danger')
        return redirect(url_for('forecast'))

    rows = db.execute(
        'SELECT * FROM forecast_item WHERE forecast_id=? ORDER BY category, sku',
        (forecast_id,)
    ).fetchall()

    all_skus = {s['sku']: s for s in load_skus()}
    items = []
    for r in rows:
        s = all_skus.get(r['sku'], {})
        items.append(dict(r) | {
            'prefix': s.get('prefix', ''),
            'moq': s.get('moq', 1200),
            'weight_grams': s.get('weight_grams', 333),
        })

    # Group by category
    by_category = {}
    for it in items:
        cat = it['category']
        by_category.setdefault(cat, []).append(it)

    total_super_sacks = sum(it['super_sacks'] or 0 for it in items)
    total_units = sum(it['final_qty'] or 0 for it in items)
    needs_order = [it for it in items if (it['final_qty'] or 0) > 0]

    forecasts = db.execute('SELECT * FROM forecast_session ORDER BY created_date DESC').fetchall()

    return render_template('view_forecast.html',
        session=session,
        items=items,
        by_category=by_category,
        total_super_sacks=round(total_super_sacks, 2),
        total_units=total_units,
        needs_order=needs_order,
        forecasts=forecasts,
        tier_reorder=TIER_REORDER,
    )


@app.route('/forecast/<int:forecast_id>/update', methods=['POST'])
def update_forecast_qty(forecast_id):
    """AJAX endpoint to update final_qty for a forecast item."""
    db = get_db()
    data = request.get_json()
    sku = data.get('sku')
    final_qty = int(data.get('final_qty', 0))

    item = db.execute('SELECT * FROM forecast_item WHERE forecast_id=? AND sku=?', (forecast_id, sku)).fetchone()
    if not item:
        return jsonify({'error': 'Not found'}), 404

    all_skus = {s['sku']: s for s in load_skus()}
    s = all_skus.get(sku, {})
    weight_g = s.get('weight_grams', 333)
    super_sacks = round(final_qty * weight_g / SUPER_SACK_GRAMS, 3)

    db.execute(
        'UPDATE forecast_item SET final_qty=?, super_sacks=? WHERE forecast_id=? AND sku=?',
        (final_qty, super_sacks, forecast_id, sku)
    )
    db.commit()

    # Recompute totals
    totals = db.execute(
        'SELECT SUM(final_qty) as total_units, SUM(super_sacks) as total_ss FROM forecast_item WHERE forecast_id=?',
        (forecast_id,)
    ).fetchone()

    return jsonify({
        'sku': sku,
        'final_qty': final_qty,
        'super_sacks': super_sacks,
        'total_units': totals['total_units'] or 0,
        'total_super_sacks': round(totals['total_ss'] or 0, 2),
    })


@app.route('/forecast/<int:forecast_id>/delete', methods=['POST'])
def delete_forecast(forecast_id):
    db = get_db()
    db.execute('DELETE FROM forecast_item WHERE forecast_id=?', (forecast_id,))
    db.execute('DELETE FROM forecast_session WHERE id=?', (forecast_id,))
    db.commit()
    flash('Forecast deleted.', 'info')
    return redirect(url_for('forecast'))


@app.route('/calculator')
def calculator():
    return render_template('calculator.html', super_sack_kg=400)


@app.route('/api/calculator', methods=['POST'])
def api_calculator():
    """Super sack calculator - given a demand shortfall, compute how many super sacks and unit mix."""
    data = request.get_json()
    units_needed = float(data.get('units_needed', 0))
    prefix = data.get('prefix', 'PP')

    all_skus = load_skus()
    sku_info = next((s for s in all_skus if s['prefix'] == prefix), None)
    if not sku_info:
        return jsonify({'error': 'Unknown prefix'}), 400

    moq = sku_info['moq']
    weight_g = sku_info['weight_grams']

    # Ceiling to MOQ
    if units_needed <= 0:
        return jsonify({'units': 0, 'super_sacks': 0, 'moq_multiples': 0})

    moq_multiples = max(1, -(-int(units_needed) // moq))
    final_units = moq_multiples * moq
    super_sacks = final_units * weight_g / SUPER_SACK_GRAMS

    result = {
        'units_needed': units_needed,
        'moq': moq,
        'moq_multiples': moq_multiples,
        'final_units': final_units,
        'super_sacks': round(super_sacks, 3),
        'weight_grams': weight_g,
    }

    # For protein pouches and stick packs, offer a mix option
    if prefix == 'PP':
        remainder_g = final_units * weight_g % SUPER_SACK_GRAMS
        if remainder_g > 0:
            stick_units = int(remainder_g / 35)
            stick_units_moq = (stick_units // 11500) * 11500
            result['mix_option'] = {
                'pouches': (moq_multiples - 1) * moq if moq_multiples > 1 else moq,
                'stick_packs': stick_units_moq,
                'stick_pack_moq': 11500,
                'stick_pack_weight_g': 35,
            }

    return jsonify(result)


@app.route('/api/tiers', methods=['GET', 'POST'])
def api_tiers():
    db = get_db()
    if request.method == 'POST':
        data = request.get_json()
        for sku, tier in data.items():
            db.execute('INSERT OR REPLACE INTO sku_tier (sku, tier) VALUES (?,?)', (sku, int(tier)))
        db.commit()
        return jsonify({'ok': True})
    rows = db.execute('SELECT sku, tier FROM sku_tier').fetchall()
    return jsonify({r['sku']: r['tier'] for r in rows})


@app.route('/api/demand_summary')
def api_demand_summary():
    """Quick demand summary for dashboard."""
    db = get_db()
    # All inventory imports (ShipHero + Amazon FBA)
    inv_rows = db.execute('SELECT id FROM inventory_import ORDER BY import_date DESC LIMIT 2').fetchall()
    inv_ids = [r['id'] for r in inv_rows] or None

    latest_sh = db.execute("SELECT * FROM sales_import WHERE source='shopify' ORDER BY import_date DESC LIMIT 1").fetchone()
    # Latest of each Amazon period
    az_rows = db.execute(
        "SELECT id FROM sales_import WHERE source='amazon' GROUP BY period_days ORDER BY import_date DESC"
    ).fetchall()
    az_ids = [r['id'] for r in az_rows]

    if not inv_ids and not latest_sh and not az_ids:
        return jsonify({'rows': []})

    rows = compute_forecast(inv_ids, latest_sh['id'] if latest_sh else None, az_ids)

    summary = [{
        'sku': r['sku'],
        'product_name': r['product_name'],
        'category': r['category'],
        'current_inventory': r['current_inventory'],
        'avg_daily_demand': r['avg_daily_demand'],
        'days_of_demand': r['days_of_demand'],
        'needs_reorder': r['needs_reorder'],
        'reorder_point': r['reorder_point'],
    } for r in rows]

    return jsonify({'rows': summary})


# ── API Credentials helpers ───────────────────────────────────────────────────

def get_creds(service: str) -> dict:
    db = get_db()
    row = db.execute('SELECT credentials FROM api_credentials WHERE service=?', (service,)).fetchone()
    if row:
        return json.loads(row['credentials'])
    return {}


def save_creds(service: str, creds: dict):
    db = get_db()
    db.execute('INSERT OR REPLACE INTO api_credentials (service, credentials) VALUES (?,?)',
               (service, json.dumps(creds)))
    db.commit()


def log_sync(service: str, status: str, message: str, records: int = 0):
    db = get_db()
    db.execute('INSERT INTO sync_log (sync_date, service, status, message, records_synced) VALUES (?,?,?,?,?)',
               (datetime.now().isoformat(), service, status, message, records))
    db.commit()


# ── Settings route ────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        service = request.form.get('service')
        if service == 'shiphero':
            save_creds('shiphero', {'token': request.form.get('token', '').strip()})
            flash('ShipHero credentials saved.', 'success')
        elif service == 'shopify':
            save_creds('shopify', {
                'store': request.form.get('store', '').strip(),
                'token': request.form.get('token', '').strip(),
            })
            flash('Shopify credentials saved.', 'success')
        elif service == 'amazon':
            save_creds('amazon', {
                'lwa_app_id': request.form.get('lwa_app_id', '').strip(),
                'lwa_client_secret': request.form.get('lwa_client_secret', '').strip(),
                'refresh_token': request.form.get('refresh_token', '').strip(),
                'marketplace_id': request.form.get('marketplace_id', 'ATVPDKIKX0DER').strip(),
                'seller_id': request.form.get('seller_id', '').strip(),
            })
            flash('Amazon credentials saved.', 'success')
        return redirect(url_for('settings'))

    sh_creds = get_creds('shiphero')
    sp_creds = get_creds('shopify')
    az_creds = get_creds('amazon')
    db = get_db()
    sync_logs = db.execute('SELECT * FROM sync_log ORDER BY sync_date DESC LIMIT 20').fetchall()

    return render_template('settings.html',
        sh_creds=sh_creds, sp_creds=sp_creds, az_creds=az_creds,
        sync_logs=sync_logs)


@app.route('/settings/test/<service>')
def test_connection(service):
    if service == 'shiphero':
        from integrations.shiphero import test_connection as tc
        creds = get_creds('shiphero')
        if not creds.get('token'):
            return jsonify({'ok': False, 'message': 'No token configured'})
        result = tc(creds['token'])
    elif service == 'shopify':
        from integrations.shopify_api import test_connection as tc
        creds = get_creds('shopify')
        if not creds.get('store') or not creds.get('token'):
            return jsonify({'ok': False, 'message': 'Store URL and token required'})
        result = tc(creds['store'], creds['token'])
    elif service == 'amazon':
        from integrations.amazon_sp import test_connection as tc
        creds = get_creds('amazon')
        if not creds.get('refresh_token'):
            return jsonify({'ok': False, 'message': 'No credentials configured'})
        result = tc(creds)
    else:
        return jsonify({'ok': False, 'message': 'Unknown service'})
    return jsonify(result)


# ── Sync routes ───────────────────────────────────────────────────────────────

@app.route('/sync/shiphero', methods=['POST'])
def sync_shiphero():
    from integrations.shiphero import fetch_inventory
    from app import parse_shiphero  # reuse SKU mapping logic
    creds = get_creds('shiphero')
    if not creds.get('token'):
        return jsonify({'ok': False, 'message': 'ShipHero token not configured. Go to Settings.'})
    try:
        raw_items = fetch_inventory(creds['token'])
    except Exception as e:
        log_sync('shiphero', 'error', str(e))
        return jsonify({'ok': False, 'message': str(e)})

    # Map ShipHero SKUs to ProDough SKUs
    merchant_to_prodough, _, _ = load_mappings()
    skus_by_sku = {s['sku']: s for s in load_skus()}
    seen = {}
    for it in raw_items:
        raw_sku = it['sku']
        prodough_sku = merchant_to_prodough.get(raw_sku, raw_sku)
        if prodough_sku not in skus_by_sku:
            match = next((k for k in skus_by_sku if k.upper() == prodough_sku.upper()), None)
            if not match:
                continue
            prodough_sku = match
        if prodough_sku in seen:
            seen[prodough_sku]['on_hand'] += it['on_hand']
            seen[prodough_sku]['available'] += it['available']
        else:
            seen[prodough_sku] = {
                'sku': prodough_sku,
                'product_name': skus_by_sku[prodough_sku]['display_name'] or it['product_name'],
                'on_hand': it['on_hand'],
                'available': it['available'],
                'warehouse': it.get('warehouse', ''),
            }

    items = list(seen.values())
    db = get_db()
    imp = db.execute(
        "INSERT INTO inventory_import (import_date, source, filename, row_count) VALUES (?,?,?,?)",
        (datetime.now().isoformat(), 'shiphero', 'API sync', len(items))
    )
    imp_id = imp.lastrowid
    for it in items:
        db.execute(
            'INSERT INTO inventory_item (import_id, sku, product_name, on_hand, available, warehouse) VALUES (?,?,?,?,?,?)',
            (imp_id, it['sku'], it['product_name'], it['on_hand'], it['available'], it['warehouse'])
        )
    db.commit()
    log_sync('shiphero', 'success', f'{len(items)} SKUs synced', len(items))
    return jsonify({'ok': True, 'message': f'ShipHero inventory synced: {len(items)} SKUs', 'records': len(items)})


@app.route('/sync/shopify', methods=['POST'])
def sync_shopify():
    from integrations.shopify_api import fetch_sales
    creds = get_creds('shopify')
    if not creds.get('store') or not creds.get('token'):
        return jsonify({'ok': False, 'message': 'Shopify credentials not configured. Go to Settings.'})

    period_days = request.get_json(silent=True, force=True) or {}
    period_days = int(period_days.get('period_days', 30))

    try:
        raw_items = fetch_sales(creds['store'], creds['token'], period_days)
    except Exception as e:
        log_sync('shopify', 'error', str(e))
        return jsonify({'ok': False, 'message': str(e)})

    # Map to ProDough SKUs
    skus_by_sku = {s['sku']: s for s in load_skus()}
    items = {}
    for it in raw_items:
        sku = it['sku'].strip()
        if sku not in skus_by_sku:
            match = next((k for k in skus_by_sku if k.upper() == sku.upper()), None)
            if not match:
                continue
            sku = match
        if sku in items:
            items[sku]['units_sold'] += it['units_sold']
        else:
            items[sku] = {'sku': sku, 'product_name': skus_by_sku[sku]['display_name'] or it['product_name'], 'units_sold': it['units_sold']}

    item_list = list(items.values())
    db = get_db()
    imp = db.execute(
        'INSERT INTO sales_import (import_date, source, period_days, filename, row_count) VALUES (?,?,?,?,?)',
        (datetime.now().isoformat(), 'shopify', period_days, 'API sync', len(item_list))
    )
    imp_id = imp.lastrowid
    for it in item_list:
        db.execute('INSERT INTO sales_item (import_id, sku, product_name, units_sold) VALUES (?,?,?,?)',
                   (imp_id, it['sku'], it['product_name'], it['units_sold']))
    db.commit()
    log_sync('shopify', 'success', f'{len(item_list)} SKUs synced ({period_days}d)', len(item_list))
    return jsonify({'ok': True, 'message': f'Shopify {period_days}d sales synced: {len(item_list)} SKUs', 'records': len(item_list)})


@app.route('/sync/amazon_inventory', methods=['POST'])
def sync_amazon_inventory():
    from integrations.amazon_sp import fetch_fba_inventory
    creds = get_creds('amazon')
    if not creds.get('refresh_token'):
        return jsonify({'ok': False, 'message': 'Amazon credentials not configured. Go to Settings.'})
    try:
        raw_items = fetch_fba_inventory(creds)
    except Exception as e:
        log_sync('amazon_inventory', 'error', str(e))
        return jsonify({'ok': False, 'message': str(e)})

    _, asin_to_prodough, _ = load_mappings()
    merchant_to_prodough, _, _ = load_mappings()
    skus_by_sku = {s['sku']: s for s in load_skus()}
    seen = {}
    for it in raw_items:
        prodough_sku = asin_to_prodough.get(it['asin']) or merchant_to_prodough.get(it['sku'])
        if not prodough_sku or prodough_sku not in skus_by_sku:
            continue
        if prodough_sku in seen:
            seen[prodough_sku]['on_hand'] += it['on_hand']
            seen[prodough_sku]['available'] += it['available']
        else:
            seen[prodough_sku] = {
                'sku': prodough_sku,
                'product_name': skus_by_sku[prodough_sku]['display_name'] or it['product_name'],
                'on_hand': it['on_hand'],
                'available': it['available'],
                'warehouse': 'Amazon FBA',
            }

    items = list(seen.values())
    db = get_db()
    imp = db.execute(
        "INSERT INTO inventory_import (import_date, source, filename, row_count) VALUES (?,?,?,?)",
        (datetime.now().isoformat(), 'amazon_fba', 'API sync', len(items))
    )
    imp_id = imp.lastrowid
    for it in items:
        db.execute('INSERT INTO inventory_item (import_id, sku, product_name, on_hand, available, warehouse) VALUES (?,?,?,?,?,?)',
                   (imp_id, it['sku'], it['product_name'], it['on_hand'], it['available'], it['warehouse']))
    db.commit()
    log_sync('amazon_inventory', 'success', f'{len(items)} SKUs synced', len(items))
    return jsonify({'ok': True, 'message': f'Amazon FBA inventory synced: {len(items)} SKUs', 'records': len(items)})


@app.route('/sync/amazon_sales', methods=['POST'])
def sync_amazon_sales():
    """Sync all three Amazon sales periods (15d, 30d, 60d) concurrently."""
    from integrations.amazon_sp import fetch_sales_report
    import threading

    creds = get_creds('amazon')
    if not creds.get('refresh_token'):
        return jsonify({'ok': False, 'message': 'Amazon credentials not configured. Go to Settings.'})

    _, asin_to_prodough, _ = load_mappings()
    skus_by_sku = {s['sku']: s for s in load_skus()}

    results = {}
    errors = {}

    def sync_period(days):
        try:
            raw = fetch_sales_report(creds, days)
            items = {}
            for it in raw:
                prodough_sku = asin_to_prodough.get(it['asin'])
                if not prodough_sku or prodough_sku not in skus_by_sku:
                    continue
                items[prodough_sku] = {
                    'sku': prodough_sku,
                    'product_name': skus_by_sku[prodough_sku]['display_name'],
                    'units_sold': it['units_sold'],
                }
            results[days] = list(items.values())
        except Exception as e:
            errors[days] = str(e)

    threads = [threading.Thread(target=sync_period, args=(d,)) for d in (15, 30, 60)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=360)

    if errors:
        msg = '; '.join(f'{d}d: {e}' for d, e in errors.items())
        log_sync('amazon_sales', 'error', msg)
        return jsonify({'ok': False, 'message': f'Amazon sales sync errors: {msg}'})

    db = get_db()
    summaries = []
    for days, items in sorted(results.items()):
        imp = db.execute(
            'INSERT INTO sales_import (import_date, source, period_days, filename, row_count) VALUES (?,?,?,?,?)',
            (datetime.now().isoformat(), 'amazon', days, 'API sync', len(items))
        )
        imp_id = imp.lastrowid
        for it in items:
            db.execute('INSERT INTO sales_item (import_id, sku, product_name, units_sold) VALUES (?,?,?,?)',
                       (imp_id, it['sku'], it['product_name'], it['units_sold']))
        summaries.append(f'{days}d: {len(items)} SKUs')
    db.commit()
    msg = 'Amazon sales synced — ' + ', '.join(summaries)
    log_sync('amazon_sales', 'success', msg, sum(len(v) for v in results.values()))
    return jsonify({'ok': True, 'message': msg})


@app.route('/sync/all', methods=['POST'])
def sync_all():
    """Trigger all available syncs sequentially and return combined status."""
    results = {}
    with app.test_request_context():
        pass
    # Delegate to individual sync endpoints
    from flask import current_app
    with current_app.test_request_context():
        for name, fn in [('shiphero', sync_shiphero), ('shopify', sync_shopify),
                          ('amazon_inventory', sync_amazon_inventory), ('amazon_sales', sync_amazon_sales)]:
            creds_key = 'shiphero' if name == 'shiphero' else ('shopify' if name == 'shopify' else 'amazon')
            c = get_creds(creds_key)
            if not c:
                results[name] = {'ok': False, 'message': 'Not configured'}
                continue
            try:
                r = fn()
                results[name] = r.get_json()
            except Exception as e:
                results[name] = {'ok': False, 'message': str(e)}
    return jsonify(results)


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)

"""Amazon SP-API integration.

Credentials required (from Seller Central → Apps & Services → Develop Apps):
  - AMAZON_LWA_APP_ID       Login with Amazon Client ID
  - AMAZON_LWA_CLIENT_SECRET
  - AMAZON_REFRESH_TOKEN    LWA Refresh Token
  - AMAZON_MARKETPLACE_ID   e.g. ATVPDKIKX0DER (US)
  - AMAZON_SELLER_ID

Setup guide:
  https://developer-docs.amazon.com/sp-api/docs/registering-as-a-developer

Uses the python-amazon-sp-api library (pip install python-amazon-sp-api).
"""

import time
import json
import io
import csv
from datetime import datetime, timedelta, timezone

from sp_api.api import Inventories, Reports
from sp_api.base import Marketplaces, Credentials, ReportType


def _credentials(creds: dict) -> Credentials:
    return Credentials(
        refresh_token=creds['refresh_token'],
        lwa_app_id=creds['lwa_app_id'],
        lwa_client_secret=creds['lwa_client_secret'],
    )


def _marketplace(creds: dict) -> Marketplaces:
    """Map marketplace_id string to Marketplaces enum. Defaults to US."""
    mapping = {
        'ATVPDKIKX0DER': Marketplaces.US,
        'A2EUQ1WTGCTBG2': Marketplaces.CA,
        'A1AM78C64UM0Y8': Marketplaces.MX,
        'A1RKKUPIHCS9HS': Marketplaces.GB,
    }
    mid = creds.get('marketplace_id', 'ATVPDKIKX0DER')
    return mapping.get(mid, Marketplaces.US)


# ── FBA Inventory ─────────────────────────────────────────────────────────────

def fetch_fba_inventory(creds: dict) -> list[dict]:
    """Fetch FBA inventory via the Inventories API.
    Returns list of {asin, sku, product_name, on_hand, available}.
    """
    inv_api = Inventories(credentials=_credentials(creds), marketplace=_marketplace(creds))
    marketplace = _marketplace(creds)

    items = []
    next_token = None

    while True:
        kwargs = {
            'details': True,
            'marketplaceIds': [marketplace.marketplace_id],
        }
        if next_token:
            kwargs['nextToken'] = next_token

        resp = inv_api.get_inventory_summary_marketplace(**kwargs)
        payload = resp.payload or {}

        for summary in payload.get('inventorySummaries', []):
            total = int(summary.get('totalQuantity') or 0)
            fulfillable = int(summary.get('fulfillableQuantity') or 0)
            items.append({
                'asin': summary.get('asin', ''),
                'sku': summary.get('sellerSku') or summary.get('sku') or '',
                'product_name': summary.get('productName') or '',
                'on_hand': total,
                'available': fulfillable,
                'warehouse': 'Amazon FBA',
            })

        next_token = payload.get('nextToken')
        if not next_token:
            break

    return items


# ── Business Report (Sales & Traffic by Child Item) ───────────────────────────

def _poll_report(reports_api, report_id: str, max_wait_s: int = 300) -> str | None:
    """Poll until report is DONE. Returns documentId or None on failure."""
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        resp = reports_api.get_report(report_id)
        status = (resp.payload or {}).get('processingStatus', '')
        if status == 'DONE':
            return (resp.payload or {}).get('reportDocumentId')
        if status in ('CANCELLED', 'FATAL'):
            raise ValueError(f'Amazon report {report_id} ended with status {status}')
        time.sleep(15)
    raise TimeoutError(f'Amazon report {report_id} did not complete within {max_wait_s}s')


def fetch_sales_report(creds: dict, period_days: int) -> list[dict]:
    """Request and download the Detail Page Sales & Traffic by Child Item report.
    Returns list of {asin, units_sold}.

    Note: Amazon report generation is asynchronous. This function blocks
    (polls every 15s) until the report is ready — typically 1-3 minutes.
    """
    marketplace = _marketplace(creds)
    reports_api = Reports(credentials=_credentials(creds), marketplace=marketplace)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=period_days)

    resp = reports_api.create_report(
        reportType=ReportType.GET_SALES_AND_TRAFFIC_REPORT,
        dataStartTime=start_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
        dataEndTime=end_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
        reportOptions={
            'dateGranularity': 'DAY',
            'asinGranularity': 'CHILD',
        },
        marketplaceIds=[marketplace.marketplace_id],
    )
    report_id = (resp.payload or {}).get('reportId')
    if not report_id:
        raise ValueError('Amazon did not return a reportId')

    document_id = _poll_report(reports_api, report_id)
    if not document_id:
        raise ValueError('Report completed but no documentId returned')

    doc_resp = reports_api.get_report_document(document_id, decrypt=True)
    raw = doc_resp.payload

    # Parse JSON response
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)

    results = []
    for item in (raw or {}).get('salesAndTrafficByAsin', []):
        asin = item.get('childAsin') or item.get('parentAsin') or ''
        units = int((item.get('salesByAsin') or {}).get('unitsOrdered') or
                    item.get('unitsOrdered') or 0)
        if asin and units > 0:
            results.append({'asin': asin, 'units_sold': units})

    return results


# ── Connection test ───────────────────────────────────────────────────────────

def test_connection(creds: dict) -> dict:
    """Quick connectivity test using the Inventories API. Returns {ok, message}."""
    try:
        marketplace = _marketplace(creds)
        inv_api = Inventories(credentials=_credentials(creds), marketplace=marketplace)
        resp = inv_api.get_inventory_summary_marketplace(
            marketplaceIds=[marketplace.marketplace_id]
        )
        count = len((resp.payload or {}).get('inventorySummaries', []))
        return {'ok': True, 'message': f'Connected to Amazon SP-API ({count} FBA items visible)'}
    except Exception as e:
        return {'ok': False, 'message': str(e)}

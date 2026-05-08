"""Shopify Admin GraphQL API integration.

Auth: Private app access token (Admin → Settings → Apps → Develop Apps).
Required scopes: read_orders, read_products.

Sales by variant are computed by aggregating paid order line items
over the requested date range — equivalent to the "Total Sales by
Product Variant" report, available on all Shopify plan tiers.
"""

import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

API_VERSION = '2024-04'


def _endpoint(store: str) -> str:
    store = store.replace('https://', '').replace('http://', '').rstrip('/')
    if not store.endswith('.myshopify.com'):
        store = f'{store}.myshopify.com'
    return f'https://{store}/admin/api/{API_VERSION}/graphql.json'


def _gql(store: str, token: str, query: str, variables: dict = None) -> dict:
    headers = {
        'X-Shopify-Access-Token': token,
        'Content-Type': 'application/json',
    }
    payload = {'query': query}
    if variables:
        payload['variables'] = variables
    resp = requests.post(_endpoint(store), json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if 'errors' in data:
        raise ValueError(f"Shopify API error: {data['errors'][0].get('message', data['errors'])}")
    return data


_ORDERS_QUERY = """
query GetOrders($query: String!, $cursor: String) {
  orders(first: 250, query: $query, after: $cursor, sortKey: CREATED_AT) {
    edges {
      node {
        id
        lineItems(first: 100) {
          edges {
            node {
              quantity
              currentQuantity
              variant {
                sku
              }
              product {
                title
              }
            }
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def fetch_sales(store: str, token: str, period_days: int) -> list[dict]:
    """Fetch net units sold by variant SKU over the last `period_days` days.
    Returns list of {sku, product_name, units_sold}.
    """
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=period_days)
    # Shopify query date filter format
    q = (f'created_at:>={start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")} '
         f'created_at:<={end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")} '
         f'financial_status:paid')

    sales: dict[str, dict] = {}
    cursor = None

    while True:
        variables = {'query': q}
        if cursor:
            variables['cursor'] = cursor
        data = _gql(store, token, _ORDERS_QUERY, variables)
        orders_data = data.get('data', {}).get('orders', {})

        for edge in orders_data.get('edges', []):
            for li_edge in edge['node'].get('lineItems', {}).get('edges', []):
                li = li_edge['node']
                sku = (li.get('variant') or {}).get('sku') or ''
                sku = sku.strip()
                if not sku:
                    continue
                qty = int(li.get('quantity') or 0)
                if qty <= 0:
                    continue
                name = (li.get('product') or {}).get('title', '')
                if sku in sales:
                    sales[sku]['units_sold'] += qty
                else:
                    sales[sku] = {'sku': sku, 'product_name': name, 'units_sold': qty}

        page_info = orders_data.get('pageInfo', {})
        if not page_info.get('hasNextPage'):
            break
        cursor = page_info.get('endCursor')

    return list(sales.values())


def test_connection(store: str, token: str) -> dict:
    """Quick connectivity test. Returns {ok, message}."""
    try:
        data = _gql(store, token,
                    '{ shop { name myshopifyDomain } }')
        shop = data.get('data', {}).get('shop', {})
        return {'ok': True, 'message': f"Connected to {shop.get('name', store)}"}
    except Exception as e:
        return {'ok': False, 'message': str(e)}

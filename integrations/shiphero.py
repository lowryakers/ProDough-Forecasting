"""ShipHero GraphQL API integration.

Auth: Bearer token from ShipHero Settings → Integrations → API.
Endpoint: https://public-api.shiphero.com/graphql
"""

import requests

ENDPOINT = 'https://public-api.shiphero.com/graphql'


def _gql(token: str, query: str, variables: dict = None) -> dict:
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
    payload = {'query': query}
    if variables:
        payload['variables'] = variables
    resp = requests.post(ENDPOINT, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if 'errors' in data:
        raise ValueError(f"ShipHero API error: {data['errors'][0].get('message', data['errors'])}")
    return data


def fetch_inventory(token: str) -> list[dict]:
    """Fetch all products with inventory from ShipHero.
    Returns list of {sku, product_name, on_hand, available, warehouse}.
    Handles cursor-based pagination automatically.
    """
    query = """
    query GetProducts($cursor: String) {
      products(first: 100, after: $cursor) {
        data(first: 100) {
          edges {
            node {
              sku
              name
              warehouse_products {
                on_hand
                available
                warehouse {
                  identifier
                }
              }
            }
          }
          page_info {
            has_next_page
            end_cursor
          }
        }
      }
    }
    """

    items = {}
    cursor = None

    while True:
        data = _gql(token, query, {'cursor': cursor} if cursor else {})
        products_data = data.get('data', {}).get('products', {}).get('data', {})
        edges = products_data.get('edges', [])

        for edge in edges:
            node = edge.get('node', {})
            sku = (node.get('sku') or '').strip()
            name = (node.get('name') or '').strip()
            if not sku:
                continue

            warehouse_products = node.get('warehouse_products') or []
            for wp in warehouse_products:
                wh = (wp.get('warehouse') or {}).get('identifier', 'Primary')
                on_hand = int(wp.get('on_hand') or 0)
                available = int(wp.get('available') or 0)
                key = f'{sku}|{wh}'
                if key in items:
                    items[key]['on_hand'] += on_hand
                    items[key]['available'] += available
                else:
                    items[key] = {
                        'sku': sku,
                        'product_name': name,
                        'on_hand': on_hand,
                        'available': available,
                        'warehouse': wh,
                    }

        page_info = products_data.get('page_info', {})
        if not page_info.get('has_next_page'):
            break
        cursor = page_info.get('end_cursor')

    return list(items.values())


def test_connection(token: str) -> dict:
    """Quick connectivity test. Returns {ok, message}."""
    try:
        data = _gql(token, '{ products(first: 1) { data(first: 1) { edges { node { sku } } } } }')
        return {'ok': True, 'message': 'Connected to ShipHero'}
    except Exception as e:
        return {'ok': False, 'message': str(e)}

"""Adapters for observed public metadata. No implicit ecommerce offer authority."""
import html
import json

from contracts import Product, ProductKey, Provenance


def adapt_amazon_2018_metadata(row, *, source_ref, row_number=None):
    asin = row.get('asin')
    if not isinstance(asin, str) or not asin.strip():
        raise ValueError('Amazon metadata requires native ASIN')
    title = row.get('title')
    brand = row.get('brand')
    category = row.get('category') or []
    if not isinstance(category, list) or any(not isinstance(x, str) for x in category):
        raise ValueError('Observed category schema is a list of strings')
    attributes = {'metadata_status': 'historical_static_snapshot'}
    for name in ['feature', 'description', 'details']:
        value = row.get(name)
        if value:
            attributes[name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if row.get('price'):
        attributes['historical_price_text'] = str(row['price'])
    return Product(
        key=ProductKey('amazon2018_luxury_beauty', asin),
        provenance=Provenance(source_ref, asin, row_number, 'amazon_2018_metadata_v1'),
        title=html.unescape(title).strip() or None if isinstance(title, str) else None,
        brand=html.unescape(brand).strip() or None if isinstance(brand, str) else None,
        category=' / '.join(html.unescape(x) for x in category if x.strip()) or None,
        attributes=attributes,
        price_missing_reason='no_current_authoritative_offer',
    )

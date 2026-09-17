"""Pure staging rules. No database writes, model calls, or real-price claims."""
import hashlib
import json
import re

VERSION = 'external-simulated-catalog-v1'
EXPECTED = {'kuaisearch': 6634118, 'multicpr': 1002822}
BASE = 4_000_000_000_000_000
WIDTH = 1_000_000_000_000
CODES = {'kuaisearch': 0, 'multicpr': 1}
LIMITS = {'source': 64, 'sourceItemId': 128, 'title': 512, 'brand': 128,
          'seller': 255, 'categoryL1': 128, 'categoryL2': 128, 'categoryL3': 128,
          'datasetRevision': 128, 'sourceLicense': 64, 'provenanceUrl': 512}
MISSING = {'', 'UNKNOWN', 'NULL', 'NONE', 'N/A', '无品牌', '其他', '其它', '其他/OTHER', '其它/OTHER', '缺失'}

def normalize(value):
    if value is not None and not isinstance(value, str):
        raise ValueError('text_field_wrong_type')
    text = ' '.join((value or '').split())
    return 'unknown' if text.upper() in MISSING else text

def native_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('invalid_id_type')
    text = str(value)
    if not re.fullmatch(r'[1-9][0-9]*', text) or int(text) >= WIDTH:
        raise ValueError('noncanonical_or_out_of_range_native_id')
    return text

def decode(source, raw):
    text = raw.decode('utf-8-sig').rstrip('\r\n')
    if source == 'kuaisearch':
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError('not_an_object')
        ident = native_id(obj.get('item_id'))
        fields = dict(title=obj.get('item_title'), brand=obj.get('brand_name'), seller=obj.get('seller_name'))
        fields.update({f'categoryL{i}': obj.get(f'category_level{i}_name') for i in (1, 2, 3)})
    elif source == 'multicpr':
        ident, title = text.split('\t', 1)
        ident = native_id(ident)
        fields = dict(title=title, brand=None, seller=None, categoryL1=None, categoryL2=None, categoryL3=None)
    else:
        raise ValueError('unsupported_source')
    return ident, {k: normalize(v) for k, v in fields.items()}

def simulated_price(source, ident):
    # 10..9999 whole yuan; arbitrary demo values, not semantic/market estimation.
    h = hashlib.sha256(f'{VERSION}|price|{source}:{ident}'.encode()).digest()
    return (10 + int.from_bytes(h[:8], 'big') % 9990) * 100

def candidate(source, ident, fields, revision, existing=None):
    pid = existing if existing is not None else BASE + CODES[source] * WIDTH + int(ident)
    return dict(id=str(pid), source=source, sourceItemId=ident, **fields,
                snapshotPriceMinor=None, currency='CNY', priceStatus='missing',
                dataNature='historical_dataset_snapshot', datasetRevision=revision,
                sourceLicense='unknown', provenanceUrl=f'urn:catalog:{source}:{ident}',
                localOffer=dict(priceMinor=simulated_price(source, ident), currency='CNY',
                                priceKind='local_simulated', sourceRevision=VERSION),
                initialStock=dict(total=10, available=10, reserved=0, sold=0),
                fieldStates={k: 'unknown' if v == 'unknown' else 'source_claim' for k, v in fields.items()},
                action='PRESERVE_EXISTING_NO_WRITES' if existing is not None else 'STAGING_ONLY')

def validate(row):
    errors = []
    for key, maximum in LIMITS.items():
        value = row[key]
        # Java @Size counts UTF-16 code units, stricter than MySQL CHAR_LENGTH for emoji.
        if len(value.encode('utf-16-le')) // 2 > maximum:
            errors.append('too_long:' + key)
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            errors.append('control_character:' + key)
        if '\ufffd' in value:
            errors.append('replacement_character:' + key)
    return errors

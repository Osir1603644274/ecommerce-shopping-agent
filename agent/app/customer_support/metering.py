"""Versioned list-price estimates; never replace absent provider usage with zero."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

PRICE_PATH = Path(__file__).with_name('pricing_20260919.json')
PRICE_BYTES = PRICE_PATH.read_bytes()
PRICE = json.loads(PRICE_BYTES)
MODELS = {'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'}


def _period(value):
    at = datetime.fromisoformat(value)
    if at.tzinfo is None: raise ValueError('timestamp requires timezone')
    at = at.astimezone(timezone.utc)
    return at, 'peak' if at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10) else 'offPeak'


def estimate(receipt, endpoint):
    base = {'cost': None, 'costStatus': 'UNKNOWN_USAGE', 'costCurrency': 'USD',
            'pricingSource': PRICE['source'], 'pricingSnapshotDate': PRICE['retrievedDate'],
            'pricingSha256': hashlib.sha256(PRICE_BYTES).hexdigest()}
    parsed = urlsplit(endpoint)
    if parsed.scheme != 'https' or parsed.hostname != 'api.deepseek.com':
        return {**base, 'costStatus': 'UNVERIFIED_PROVIDER_PRICING'}
    if receipt.get('resolvedModel') not in MODELS:
        return {**base, 'costStatus': 'UNKNOWN_BILLED_MODEL'}
    usage = receipt.get('usage')
    if not isinstance(usage, dict): return base
    prompt, output = usage.get('prompt_tokens'), usage.get('completion_tokens')
    if any(type(n) is not int or n < 0 for n in (prompt, output)): return base
    total = usage.get('total_tokens')
    if total is not None and (type(total) is not int or total != prompt + output):
        return {**base, 'costStatus': 'INCONSISTENT_USAGE'}
    try:
        began, period = _period(receipt['startedAt'])
        ended, end_period = _period(receipt['finishedAt'])
        if not 0 <= (ended - began).total_seconds() <= 300 or period != end_period:
            return {**base, 'costStatus': 'PRICE_WINDOW_AMBIGUOUS'}
    except (KeyError, TypeError, ValueError):
        return {**base, 'costStatus': 'UNKNOWN_CALL_TIME'}
    rates = {k: Decimal(str(v)) for k, v in PRICE['rates'][period].items()}
    base.update(pricingPeriod=period)
    hit, miss = usage.get('prompt_cache_hit_tokens'), usage.get('prompt_cache_miss_tokens')
    if hit is None:
        details = usage.get('prompt_tokens_details')
        hit = details.get('cached_tokens') if isinstance(details, dict) else None
    if hit is None:
        lo = (prompt * rates['cacheHit'] + output * rates['output']) / Decimal(1000000)
        hi = (prompt * rates['cacheMiss'] + output * rates['output']) / Decimal(1000000)
        return {**base, 'costStatus': 'UNKNOWN_CACHE_SPLIT', 'costRange': [str(lo), str(hi)]}
    if type(hit) is not int or not 0 <= hit <= prompt or (miss is not None and (type(miss) is not int or miss != prompt-hit)):
        return {**base, 'costStatus': 'INCONSISTENT_USAGE'}
    amount = (hit * rates['cacheHit'] + (prompt-hit) * rates['cacheMiss'] + output * rates['output']) / Decimal(1000000)
    return {**base, 'cost': str(amount), 'costStatus': 'LIST_PRICE_ESTIMATE'}

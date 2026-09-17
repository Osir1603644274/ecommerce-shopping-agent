"""Preserve Java BIGINT identity across JSON and JavaScript without rounding."""
def browser_ids(value):
    if isinstance(value, list):
        return [browser_ids(item) for item in value]
    if isinstance(value, dict):
        return {key: str(item) if key in {'id', 'itemId', 'productId', 'stockId'}
                and type(item) is int and abs(item) > 9007199254740991 else browser_ids(item)
                for key, item in value.items()}
    return value

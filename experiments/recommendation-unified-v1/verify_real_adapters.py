"""Verify canonical identity/hydration using one real record from each source."""
import gzip
import hashlib
import json
from pathlib import Path

from contracts import (CandidateScope, RecommendationResult, adapt_kuaisearch_lite_item,
                       build_catalog, validate_recommendation_result)
from source_adapters import adapt_amazon_2018_metadata


def main():
    root = Path('D:/agent-datasets/recommendation-unified-v1/source-probe')
    kuai_path = Path('D:/agent-datasets/kuaisearch-lite-09807c773ce67360ed8df30842e372182fcf7ad9/items_lite.train.jsonl')
    with kuai_path.open('rb') as f:
        kuai_bytes = next(f)
        kuai = adapt_kuaisearch_lite_item(json.loads(kuai_bytes), source_ref=str(kuai_path), row_number=1)
    with gzip.open(root / 'Luxury_Beauty_5.json.gz', 'rt', encoding='utf-8') as f:
        review = json.loads(next(f))
    amazon = None
    with gzip.open(root / 'meta_Luxury_Beauty.json.gz', 'rb') as f:
        for number, line in enumerate(f, 1):
            row = json.loads(line)
            if row['asin'] == review['asin']:
                amazon = adapt_amazon_2018_metadata(row, source_ref=str(root / 'meta_Luxury_Beauty.json.gz'), row_number=number)
                amazon_record_sha256 = hashlib.sha256(line).hexdigest()
                break
    assert amazon is not None
    catalog = build_catalog([kuai, amazon])
    results = []
    for product in [kuai, amazon]:
        scope = CandidateScope(product.key.source, 'real_adapter_check_v1', 'revision1')
        result = RecommendationResult(scope, (product.key,))
        hydrated, = validate_recommendation_result(result, scope, catalog)
        assert hydrated.title and hydrated.price_minor is None
        results.append({'source': product.key.source, 'item_id': product.key.item_id,
                        'title': hydrated.title, 'price_minor': hydrated.price_minor,
                        'price_missing_reason': hydrated.price_missing_reason,
                        'source_record_row': hydrated.provenance.row_number})
    wrong_scope = CandidateScope(kuai.key.source, 'real_adapter_check_v1', 'revision1')
    try:
        validate_recommendation_result(RecommendationResult(wrong_scope, (amazon.key,)), wrong_scope, catalog)
    except ValueError:
        cross_source_rejected = True
    else:
        raise AssertionError('Cross-source result accepted')
    output = {
        'status': 'REAL_RECORD_ADAPTER_CHECK_PASSED_NOT_AGENT_E2E',
        'real_records': results, 'cross_source_result_rejected': cross_source_rejected,
        'kuaisearch_record_sha256': hashlib.sha256(kuai_bytes).hexdigest(),
        'amazon_record_sha256': amazon_record_sha256,
        'amazon_review_item_join': review['asin'] == amazon.key.item_id,
        'business_database_written': False,
    }
    dest = Path('docs/experiments/recommendation-unified-v1-20260916/REAL_RECORD_ADAPTER_CHECK.json')
    dest.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()

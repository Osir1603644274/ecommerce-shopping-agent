from evaluation.shopping_memory_v14_public_validation_quality_v1 import (
    DATASET_SHA256,
    MEMORY_WEIGHT,
)


def test_validation_quality_authority_is_frozen():
    assert MEMORY_WEIGHT == 0.08
    assert DATASET_SHA256 == "7aa11b18e4d957634c1d79ff73341d2729bb4c241ef343435ecb08f670381dec"

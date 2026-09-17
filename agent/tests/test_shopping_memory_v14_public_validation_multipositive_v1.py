from evaluation.shopping_memory_v14_public_validation_multipositive_v1 import (
    CANDIDATE_DEPTH,
    VALIDATION_SHA256,
)


def test_validation_authority_and_depth_are_frozen():
    assert CANDIDATE_DEPTH == 200
    assert VALIDATION_SHA256 == "cbd91240d733c7c3a7bf507e155ce181f28829a8c1c248aacaa44a4be09185b3"

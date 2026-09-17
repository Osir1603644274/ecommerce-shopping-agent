from evaluation.shopping_memory_v14_multipositive_depth_diagnostic_v1 import first_rank_at_least


def test_first_rank_at_least_is_one_based_and_zero_when_absent():
    assert first_rank_at_least([0, 1, 3, 2], 3) == 3
    assert first_rank_at_least([0, 1, 3, 2], 2) == 3
    assert first_rank_at_least([0, 1, 3, 2], 1) == 2
    assert first_rank_at_least([0, 0], 1) == 0

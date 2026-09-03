"""Prepare deterministic MovieLens train / validation / test case splits."""

from recommendation.movielens import convert_movielens_splits


if __name__ == "__main__":
    case_splits = convert_movielens_splits()
    counts = case_splits["counts"]
    print(
        "Generated MovieLens case splits: "
        f"train={counts['train']}, "
        f"validation={counts['validation']}, "
        f"test={counts['test']}, "
        f"total={counts['total']}"
    )
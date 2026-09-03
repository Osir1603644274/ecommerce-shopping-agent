"""Build the FunRec ItemCF similarity table from processed MovieLens cases."""

from recommendation.evaluation import load_cases
from recommendation.itemcf import (
    DEFAULT_SIMILARITY_PATH,
    build_item_similarity_table,
    save_similarity_table,
)


if __name__ == "__main__":
    cases = load_cases()
    table = build_item_similarity_table(cases)
    save_similarity_table(table)
    print(f"Saved ItemCF similarity table to {DEFAULT_SIMILARITY_PATH}")
    print(
        "Summary: "
        f"cases={table['caseCount']}, "
        f"contributingCases={table['contributingCaseCount']}, "
        f"items={table['itemCount']}, "
        f"topN={table['topN']}"
    )
"""Prepare a small Yelp local-life sample for REC-DATA."""

from __future__ import annotations

import argparse
from pathlib import Path

from recommendation.yelp import DEFAULT_OUTPUT_PATH, DEFAULT_RAW_DIR, convert_yelp_sample


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Yelp JSONL files into a local-life processed sample.")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help="Directory containing Yelp raw JSONL files.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH, help="Processed sample JSON output path.")
    parser.add_argument("--city", default=None, help="Optional exact Yelp city filter, for example Philadelphia.")
    parser.add_argument("--max-businesses", type=int, default=300, help="Maximum number of businesses to keep.")
    parser.add_argument("--max-reviews-per-shop", type=int, default=30, help="Maximum number of reviews per sampled shop.")
    parser.add_argument("--min-demo-user-behaviors", type=int, default=10, help="Minimum behavior count required to map a user to demo-user-1.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sample = convert_yelp_sample(
        raw_dir=args.raw_dir,
        output_path=args.output,
        city=args.city,
        max_businesses=args.max_businesses,
        max_reviews_per_shop=args.max_reviews_per_shop,
        min_demo_user_behaviors=args.min_demo_user_behaviors,
    )
    metadata = sample["metadata"]
    print(
        "Generated Yelp sample: "
        f"{metadata['businessCount']} shops, "
        f"{metadata['reviewCount']} reviews, "
        f"{metadata['userBehaviorCount']} user behaviors, "
        f"demoUserId={metadata['demoUserId']}. "
        f"Output: {args.output}"
    )


if __name__ == "__main__":
    main()
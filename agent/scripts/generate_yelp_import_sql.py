"""Generate MySQL import SQL from the processed Yelp local-life sample."""

from __future__ import annotations

import argparse
from pathlib import Path

from recommendation.yelp_sql import DEFAULT_SAMPLE_PATH, DEFAULT_SQL_OUTPUT_PATH, save_yelp_import_sql


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate MySQL SQL from a processed Yelp local-life sample.")
    parser.add_argument("--sample-path", type=Path, default=DEFAULT_SAMPLE_PATH, help="Processed Yelp sample JSON path.")
    parser.add_argument("--output", type=Path, default=DEFAULT_SQL_OUTPUT_PATH, help="SQL output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    save_yelp_import_sql(args.sample_path, args.output)
    print(f"Generated {args.output}")


if __name__ == "__main__":
    main()
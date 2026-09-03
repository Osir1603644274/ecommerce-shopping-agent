"""Generate SQL that fills review.content_zh with faithful Yelp translations."""

from __future__ import annotations

import argparse
from pathlib import Path

from recommendation.yelp_zh import (
    DEFAULT_SAMPLE_PATH,
    DEFAULT_ZH_SQL_OUTPUT_PATH,
    save_yelp_content_zh_sql,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_ZH_SQL_OUTPUT_PATH)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    sql = save_yelp_content_zh_sql(args.sample, args.output, limit=args.limit)
    update_count = sql.count("WHERE id = 'yelp-")
    print(f"Generated {update_count} Yelp Chinese translation updates: {args.output}")


if __name__ == "__main__":
    main()

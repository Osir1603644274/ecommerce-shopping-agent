from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
from openai import AsyncOpenAI

from recommendation.yelp_full_translation import (
    TRANSLATION_SYSTEM_PROMPT,
    append_checkpoint,
    build_translation_batches,
    build_translation_update_sql,
    cached_translation,
    checkpoint_record,
    load_checkpoint,
    parse_translation_response,
    translation_user_message,
)
from app.settings import settings


DEFAULT_CHECKPOINT = (
    Path(__file__).resolve().parents[1]
    / "translation_runs"
    / "yelp_full_translation.jsonl"
)
MYSQL_CONTAINER = "local-life-mysql"


def load_pending_reviews() -> list[dict[str, Any]]:
    response = httpx.get(
        f"{settings.backend_base_url}/api/reviews",
        timeout=30.0,
    )
    response.raise_for_status()
    reviews = response.json()["data"]
    return [
        review
        for review in reviews
        if review.get("source") == "yelp"
        and review.get("translationStatus") in {"pending", "failed"}
    ]


def apply_translations_to_mysql(translations: dict[str, str]) -> None:
    if not translations:
        return
    sql = build_translation_update_sql(translations)
    local_path: Path | None = None
    container_path = "/tmp/yelp_translation_batch.sql"
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            suffix=".sql",
            delete=False,
        ) as handle:
            handle.write(sql)
            local_path = Path(handle.name)
        subprocess.run(
            ["docker", "cp", str(local_path), f"{MYSQL_CONTAINER}:{container_path}"],
            check=True,
            capture_output=True,
        )
        result = subprocess.run(
            [
                "docker",
                "exec",
                MYSQL_CONTAINER,
                "sh",
                "-lc",
                "mysql --default-character-set=utf8mb4 "
                '-u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" '
                f"< {container_path}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "MySQL translation batch failed: "
                + (result.stderr.strip() or result.stdout.strip())
            )
    finally:
        if local_path is not None:
            local_path.unlink(missing_ok=True)
        subprocess.run(
            ["docker", "exec", MYSQL_CONTAINER, "rm", "-f", container_path],
            check=False,
            capture_output=True,
        )


async def request_translation(
    client: AsyncOpenAI,
    reviews: list[dict[str, Any]],
    *,
    max_attempts: int,
) -> dict[str, str]:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.chat.completions.create(
                model=settings.deepseek_model,
                messages=[
                    {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
                    {"role": "user", "content": translation_user_message(reviews)},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            choice = response.choices[0]
            if choice.finish_reason != "stop":
                raise ValueError(f"unexpected finish reason: {choice.finish_reason}")
            return parse_translation_response(
                choice.message.content or "",
                reviews,
            )
        except Exception as exc:
            last_error = exc
            if attempt < max_attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))

    if len(reviews) > 1:
        midpoint = len(reviews) // 2
        left, right = await asyncio.gather(
            request_translation(client, reviews[:midpoint], max_attempts=max_attempts),
            request_translation(client, reviews[midpoint:], max_attempts=max_attempts),
        )
        return {**left, **right}
    raise RuntimeError(
        f"translation failed for {reviews[0]['reviewId']}: {last_error}"
    ) from last_error


async def run(args: argparse.Namespace) -> None:
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")

    pending = load_pending_reviews()
    if args.limit is not None:
        pending = pending[: args.limit]
    checkpoints = {} if args.ignore_checkpoint else load_checkpoint(args.checkpoint)

    cached: dict[str, str] = {}
    uncached: list[dict[str, Any]] = []
    for review in pending:
        translated = cached_translation(review, checkpoints)
        if translated is None:
            uncached.append(review)
        else:
            cached[str(review["reviewId"])] = translated

    if cached and not args.dry_run:
        for batch in build_translation_batches(
            [
                {"reviewId": review_id, "content": translated}
                for review_id, translated in cached.items()
            ],
            max_chars=args.batch_max_chars,
            max_items=args.batch_max_items,
        ):
            apply_translations_to_mysql(
                {item["reviewId"]: item["content"] for item in batch}
            )
    print(
        f"pending={len(pending)} cached={len(cached)} "
        f"to_translate={len(uncached)} dry_run={args.dry_run}"
    )
    if not uncached:
        return

    batches = build_translation_batches(
        uncached,
        max_chars=args.batch_max_chars,
        max_items=args.batch_max_items,
    )
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=120.0,
        max_retries=0,
    )
    review_by_id = {
        str(review["reviewId"]): review for review in uncached
    }
    completed = 0
    for start in range(0, len(batches), args.concurrency):
        wave = batches[start : start + args.concurrency]
        results = await asyncio.gather(
            *(
                request_translation(
                    client,
                    batch,
                    max_attempts=args.max_attempts,
                )
                for batch in wave
            ),
            return_exceptions=True,
        )
        first_error: Exception | None = None
        for translations in results:
            if isinstance(translations, Exception):
                first_error = first_error or translations
                continue
            records = [
                checkpoint_record(
                    review_by_id[review_id],
                    translated,
                    model=settings.deepseek_model,
                )
                for review_id, translated in translations.items()
            ]
            if not args.dry_run:
                append_checkpoint(args.checkpoint, records)
                apply_translations_to_mysql(translations)
            completed += len(translations)
        print(
            f"translated_this_run={completed}/{len(uncached)} "
            f"waves={min(start + args.concurrency, len(batches))}/{len(batches)}"
        )
        if first_error is not None:
            raise first_error
    await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Faithfully translate pending Yelp reviews into content_zh."
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-max-chars", type=int, default=6000)
    parser.add_argument("--batch-max-items", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--ignore-checkpoint", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    for name in ("batch_max_chars", "batch_max_items", "concurrency", "max_attempts"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    return args


if __name__ == "__main__":
    asyncio.run(run(parse_args()))

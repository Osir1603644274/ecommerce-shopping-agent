from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from document_paths import DocumentLocations

ROOT = Path(__file__).resolve().parents[1]
LOCATIONS = DocumentLocations(ROOT)
LINK_PATTERN = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
SKIPPED_PREFIXES = ("http://", "https://", "mailto:", "tel:", "data:")


def markdown_files() -> list[Path]:
    files = [ROOT / "README.md"]
    files.extend(
        path
        for path in (ROOT / "docs").rglob("*.md")
        if "archive" not in path.relative_to(ROOT / "docs").parts
        # Handoff evidence directories are immutable point-in-time copies. Their
        # relative links resolve in the source tree, not inside the copied packet.
        and not (
            "handoffs" in path.relative_to(ROOT / "docs").parts
            and "evidence" in path.relative_to(ROOT / "docs").parts
        )
    )
    files.extend((ROOT / "review").rglob("*.md"))
    # Check newly archived originals as well, resolving their unchanged links
    # from their former location. Do not silently exclude the moved documents.
    files.extend(ROOT / e["current"] for e in LOCATIONS.entries if e["preserveBytes"] and e["current"].endswith(".md"))
    return sorted(set(files))


def links_outside_code_fences(path: Path) -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []
    in_fence = False
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        results.extend(
            (line_number, match.group(1).strip())
            for match in LINK_PATTERN.finditer(line)
        )
    return results


def local_target(source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip("<>")
    if not target or target.startswith(("#", "/")):
        return None
    if target.lower().startswith(SKIPPED_PREFIXES):
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    decoded = unquote(parsed.path)
    if not decoded:
        return None
    original_source = LOCATIONS.reference_source(source)
    return LOCATIONS.resolve(original_source.parent / decoded)


def main() -> int:
    failures: list[str] = []
    for source in markdown_files():
        for line_number, raw_target in links_outside_code_fences(source):
            target = local_target(source, raw_target)
            if target is not None and not target.exists():
                relative_source = source.relative_to(ROOT)
                failures.append(f"{relative_source}:{line_number}: {raw_target}")
    if failures:
        print("Broken local Markdown links:")
        print("\n".join(failures))
        return 1
    print(f"Markdown link check passed ({len(markdown_files())} files; archived references resolved through document-locations.json).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

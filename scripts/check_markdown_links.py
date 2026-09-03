from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
LINK_PATTERN = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
SKIPPED_PREFIXES = ("http://", "https://", "mailto:", "tel:", "data:")


def markdown_files() -> list[Path]:
    files = [ROOT / "README.md"]
    files.extend((ROOT / "docs").rglob("*.md"))
    files.extend((ROOT / "review").rglob("*.md"))
    return sorted(files)


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
    return (source.parent / decoded).resolve()


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
    print(f"Markdown link check passed ({len(markdown_files())} active files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

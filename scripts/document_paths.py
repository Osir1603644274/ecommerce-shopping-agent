"""Resolve archived document locations without rewriting historical evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DocumentLocations:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root.resolve()
        manifest = self.root / "docs/governance/document-locations.json"
        # Public snapshots need no private archive mapping.
        data = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {"schemaVersion": 1, "entries": []}
        if data.get("schemaVersion") != 1:
            raise ValueError("Unsupported document-locations schema")
        self.entries = data["entries"]
        self.by_original: dict[str, dict] = {}
        self.by_current: dict[str, dict] = {}
        for entry in self.entries:
            for field in ("original", "current", "relativeLinksFrom"):
                value = entry[field]
                resolved = (self.root / value).resolve()
                if not value.startswith("docs/") or not resolved.is_relative_to(self.root / "docs"):
                    raise ValueError(f"Document path outside docs: {value}")
            if entry["original"] in self.by_original or entry["current"] in self.by_current:
                raise ValueError("Duplicate document mapping")
            self.by_original[entry["original"]] = entry
            self.by_current[entry["current"]] = entry

    def relative(self, path: Path) -> str | None:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None

    def resolve(self, target: Path) -> Path:
        target = target.resolve()
        if target.exists():
            return target
        entry = self.by_original.get(self.relative(target))
        return self.root / entry["current"] if entry else target

    def reference_source(self, source: Path) -> Path:
        entry = self.by_current.get(self.relative(source))
        return self.root / entry["relativeLinksFrom"] if entry else source

    def verify(self) -> dict:
        errors = []
        unchanged = 0
        for entry in self.entries:
            target = self.root / entry["current"]
            if not target.is_file():
                errors.append(f"Missing archive: {entry['current']}")
                continue
            if (self.root / entry["original"]).exists():
                errors.append(f"Old path reappeared: {entry['original']}")
            if entry["preserveBytes"]:
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if digest != entry["sha256BeforeMove"]:
                    errors.append(f"Original changed: {entry['current']}")
                else:
                    unchanged += 1
        return {"mappedDocuments": len(self.entries), "unchangedHistoricalOriginals": unchanged, "errors": errors, "status": "FAIL" if errors else "PASS"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    locations = DocumentLocations()
    if args.check:
        result = locations.verify()
        print(json.dumps(result, ensure_ascii=False))
        return int(bool(result["errors"]))
    if not args.path:
        parser.error("Provide an old document path or --check")
    target = locations.resolve(ROOT / args.path)
    if not target.is_file():
        parser.error(f"Document not found: {args.path}")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

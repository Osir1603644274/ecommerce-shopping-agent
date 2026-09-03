from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


AGENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = AGENT_ROOT.parent


DISTRICTS = (
    "东城区", "西城区", "朝阳区", "海淀区", "丰台区", "石景山区", "门头沟区", "房山区",
    "通州区", "顺义区", "昌平区", "大兴区", "怀柔区", "平谷区", "密云区", "延庆区",
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _load_staging(
    output_root: Path,
) -> tuple[Path, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    versions = sorted(path for path in output_root.iterdir() if path.is_dir())
    if len(versions) != 1:
        raise ValueError(f"expected exactly one Beijing staging version, found {len(versions)}")
    version_dir = versions[0]
    manifest = json.loads((version_dir / "manifest.json").read_text(encoding="utf-8"))
    candidates = json.loads((version_dir / "park_candidates_100.json").read_text(encoding="utf-8"))
    museums = _load_jsonl(version_dir / "museums.jsonl")
    return version_dir, manifest, candidates, museums


def _museum_district(address: str) -> str | None:
    return next((district for district in DISTRICTS if district in address), None)


def build_catalog(
    manifest: dict[str, Any],
    candidates: list[dict[str, Any]],
    museums: list[dict[str, Any]],
) -> dict[str, Any]:
    places = []
    for candidate in sorted(candidates, key=lambda item: item["candidate_rank"]):
        places.append(
            {
                "id": candidate["candidate_id"],
                "kind": "park",
                "name": candidate["name"],
                "district": candidate["district"],
                "address": candidate["address"],
                "location": {
                    "longitude": candidate["longitude"],
                    "latitude": candidate["latitude"],
                    "coordinateSystem": candidate["coordinate_system"],
                },
                "park": {
                    "type": candidate["park_type"],
                    "level": candidate["park_level"],
                },
                "contact": {
                    "phone": candidate["phone"] or None,
                    "governingUnit": candidate["governing_unit"] or None,
                },
                "facilities": [
                    {
                        "name": facility["name"],
                        "status": facility["status"],
                        "signage": facility["signage"],
                        "signageStatus": facility["signage_status"],
                    }
                    for facility in candidate["facilities"]
                ],
                "quality": {
                    "candidateRank": candidate["candidate_rank"],
                    "candidateScore": candidate["candidate_score"],
                    "matchMethod": candidate["match_method"],
                    "matchScore": candidate["match_score"],
                    "flags": candidate["quality_flags"],
                },
                "sourceRefs": candidate["source_refs"],
            }
        )

    for museum in sorted(museums, key=lambda item: int(item["source_id"])):
        places.append(
            {
                "id": f"beijing-museum-{museum['source_id']}",
                "kind": "museum",
                "name": museum["name"],
                "district": _museum_district(museum["address"]),
                "address": museum["address"],
                "location": None,
                "museum": {"postcode": museum["postcode"]},
                "contact": {
                    "phone": museum["phone"],
                    "governingUnit": None,
                },
                "facilities": [],
                "quality": {
                    "candidateRank": 100 + int(museum["source_id"]),
                    "candidateScore": 0,
                    "matchMethod": "official_registry_record",
                    "matchScore": 1.0,
                    "flags": ["official_registry"],
                },
                "sourceRefs": {"open_museums": museum["source_id"]},
            }
        )

    sources = {}
    for key in ("official_parks", "park_accessibility", "open_museums"):
        source = manifest["sources"][key]
        sources[key] = {
            "provider": source["provider"],
            "sourceVersion": source["source_version"],
            "sourceUpdatedOn": source["source_updated_on"],
            "sourceUrl": source["source_url"],
            "sha256": source["sha256"],
        }
    return {
        "catalogVersion": "beijing-places-v2",
        "sourceDatasetVersion": manifest["dataset_version"],
        "recordCount": len(places),
        "coordinateSystems": {"park": "BD-09", "museum": None},
        "scope": "demo_snapshot_not_realtime",
        "recordCountsByKind": {"park": len(candidates), "museum": len(museums)},
        "sources": sources,
        "places": places,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish reviewed Beijing parks and museums as a runtime demo catalog."
    )
    parser.add_argument(
        "--staging-root",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "processed" / "beijing",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=AGENT_ROOT / "app" / "place_data" / "data" / "beijing_places_v2.json",
    )
    args = parser.parse_args()

    _, manifest, candidates, museums = _load_staging(args.staging_root.resolve())
    catalog = build_catalog(manifest, candidates, museums)
    if catalog["recordCountsByKind"] != {"park": 100, "museum": 190}:
        raise ValueError(f"unexpected place counts: {catalog['recordCountsByKind']}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output.resolve()),
        "catalogVersion": catalog["catalogVersion"],
        "recordCount": catalog["recordCount"],
        "recordCountsByKind": catalog["recordCountsByKind"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .parsers import (
    locate_source_files,
    parse_accessibility_parks,
    parse_museums,
    parse_official_parks,
    parse_toilets,
    sha256_file,
)


DATASET_VERSION = "parks-2025__access-2024-05-27__museums-2023-02-14__toilets-2022"
SOURCE_METADATA = {
    "official_parks": {
        "source_version": "2025", "source_updated_on": "2025-05-29",
        "coordinate_system": None, "provider": "北京市园林绿化局",
        "source_url": "https://yllhj.beijing.gov.cn/zwgk/2024nzcwj/2024nqtwj/202505/t20250529_4101774.shtml",
    },
    "park_accessibility": {
        "source_version": "2024-05-27", "source_updated_on": "2024-05-27",
        "coordinate_system": "BD-09", "provider": "北京市残疾人联合会",
        "source_url": "https://data.beijing.gov.cn/zyml/wnkfsj/b29da708521a4139b02e7ee5c5b9c63f.htm",
    },
    "open_museums": {
        "source_version": "2023-02-14", "source_updated_on": "2023-02-14",
        "coordinate_system": None, "provider": "北京市文物局",
        "source_url": "https://data.beijing.gov.cn/zyml/wnkfsj/b3d9e51742884e1ebdfe7269d4db9d94.htm",
    },
    "public_toilets": {
        "source_version": "2022", "source_updated_on": "2024-01-24",
        "coordinate_system": "unknown_beijing_source", "provider": "北京市城市管理委员会",
        "source_url": "https://data.beijing.gov.cn/zyml/wnkfsj/eb2b9eb72f1b446a9525d4f63bfe0f3e.htm",
    },
}
LEVEL_WEIGHT = {"一级": 40, "二级": 30, "三级": 20, "四级": 10}
TYPE_WEIGHT = {
    "历史名园": 8, "综合公园": 7, "自然（类）公园": 6,
    "专类公园": 5, "生态公园": 4, "社区公园": 3, "游园": 2,
}
DISTRICT_RE = re.compile(
    r"(东城区|西城区|朝阳区|海淀区|丰台区|石景山区|门头沟区|房山区|通州区|"
    r"顺义区|昌平区|大兴区|怀柔区|平谷区|密云区|延庆区|经济开发区|经开区)"
)


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[\s·•—－_，,。.;；:：、/\\（）()\[\]【】《》<>\-]", "", value)


def _name_aliases(value: str) -> list[str]:
    aliases = [normalize_name(value)]
    before_parenthesis = re.split(r"[（(]", value, maxsplit=1)[0]
    if before_parenthesis != value and any(word in before_parenthesis for word in ("公园", "绿地", "广场", "景区")):
        aliases.append(normalize_name(before_parenthesis))
    for prefix in ("北京市", "北京地区"):
        if value.startswith(prefix):
            aliases.append(normalize_name(value[len(prefix):]))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _canonical_district(record: dict[str, Any]) -> str | None:
    district = record.get("district") or ""
    if district in {"市公园管理中心", "经济开发区"}:
        match = DISTRICT_RE.search(record.get("address") or "")
        if match:
            district = match.group(1)
    district = district.replace("经开区", "经济开发区")
    return district or None


def match_parks(
    official: list[dict[str, Any]], accessibility: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_alias: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for park in official:
        for alias in _name_aliases(park["name"]):
            by_alias[alias].append(park)

    matches: list[dict[str, Any]] = []
    official_names = [(normalize_name(item["name"]), item) for item in official]
    for access in accessibility:
        aliases = _name_aliases(access["name"])
        candidates: dict[str, dict[str, Any]] = {}
        full_name_key = normalize_name(access["name"])
        for alias in aliases:
            for park in by_alias.get(alias, []):
                candidates[park["source_id"]] = park

        district = _canonical_district(access)
        district_candidates = [
            park for park in candidates.values() if _canonical_district(park) == district
        ]
        resolved = district_candidates if len(district_candidates) == 1 else list(candidates.values())
        if len(resolved) == 1:
            park = resolved[0]
            method = "exact_normalized_name" if normalize_name(park["name"]) == full_name_key else "exact_alias_name"
            if _canonical_district(park) == district:
                method += "_district"
            matches.append({
                "official_source_id": park["source_id"],
                "accessibility_source_id": access["source_id"],
                "match_method": method,
                "match_score": 1.0 if method.startswith("exact_normalized") else 0.98,
                "review_status": "accepted",
            })
            continue
        if len(resolved) > 1:
            matches.append({
                "official_source_id": None, "accessibility_source_id": access["source_id"],
                "match_method": "ambiguous_exact_name", "match_score": None,
                "review_status": "needs_review",
                "candidate_official_ids": sorted(park["source_id"] for park in resolved),
            })
            continue

        # Fuzzy results are suggestions only and can never enter the first publish batch.
        access_key = aliases[-1]
        suggestions = []
        for official_key, park in official_names:
            if district and _canonical_district(park) != district:
                continue
            score = SequenceMatcher(None, access_key, official_key).ratio()
            if score >= 0.88:
                suggestions.append((score, park))
        suggestions.sort(key=lambda item: (-item[0], int(item[1]["source_id"])))
        best = suggestions[0] if suggestions else None
        matches.append({
            "official_source_id": best[1]["source_id"] if best else None,
            "accessibility_source_id": access["source_id"],
            "match_method": "fuzzy_suggestion" if best else "unmatched",
            "match_score": round(best[0], 4) if best else None,
            "review_status": "needs_review",
        })
    accepted_by_official: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for match in matches:
        if match["review_status"] == "accepted":
            accepted_by_official[match["official_source_id"]].append(match)
    for duplicate_matches in accepted_by_official.values():
        if len(duplicate_matches) < 2:
            continue
        for match in duplicate_matches:
            match["match_method"] = "duplicate_accessibility_records_for_official"
            match["review_status"] = "needs_review"
    return matches


def select_park_candidates(
    official: list[dict[str, Any]],
    accessibility: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    limit: int = 100,
) -> list[dict[str, Any]]:
    official_by_id = {item["source_id"]: item for item in official}
    access_by_id = {item["source_id"]: item for item in accessibility}
    coordinate_names: dict[tuple[float, float], set[str]] = defaultdict(set)
    for item in accessibility:
        coordinate_names[(item["longitude"], item["latitude"])].add(normalize_name(item["name"]))

    eligible: list[dict[str, Any]] = []
    for match in matches:
        if match["review_status"] != "accepted":
            continue
        official_park = official_by_id[match["official_source_id"]]
        access_park = access_by_id[match["accessibility_source_id"]]
        coordinate_key = (access_park["longitude"], access_park["latitude"])
        if len(coordinate_names[coordinate_key]) > 1:
            continue
        district = _canonical_district(access_park) or _canonical_district(official_park)
        score = (
            LEVEL_WEIGHT.get(official_park["park_level"], 0)
            + TYPE_WEIGHT.get(official_park["park_type"], 0)
            + min(access_park["facility_row_count"], 7)
            + int(match["match_score"] * 10)
        )
        eligible.append({
            "candidate_id": f"beijing-park-{official_park['source_id']}",
            "name": official_park["name"], "place_kind": "park", "district": district,
            "address": official_park["address"], "park_type": official_park["park_type"],
            "park_level": official_park["park_level"], "phone": official_park["phone"],
            "governing_unit": official_park["governing_unit"],
            "longitude": access_park["longitude"], "latitude": access_park["latitude"],
            "coordinate_system": "BD-09", "facilities": access_park["facilities"],
            "source_refs": {
                "official_parks": official_park["source_id"],
                "park_accessibility": access_park["source_id"],
            },
            "match_method": match["match_method"], "match_score": match["match_score"],
            "candidate_score": score,
            "quality_flags": ["official_registry", "source_coordinate", "exact_name_match"],
        })

    eligible.sort(key=lambda item: (-item["candidate_score"], int(item["source_refs"]["official_parks"])))
    # Seed one high-quality record per district, then fill by the same global score.
    chosen: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for district in sorted({item["district"] for item in eligible if item["district"]}):
        item = next(candidate for candidate in eligible if candidate["district"] == district)
        chosen.append(item)
        seen_ids.add(item["candidate_id"])
        if len(chosen) == limit:
            break
    for item in eligible:
        if len(chosen) == limit:
            break
        if item["candidate_id"] not in seen_ids:
            chosen.append(item)
            seen_ids.add(item["candidate_id"])
    for rank, item in enumerate(chosen, start=1):
        item["candidate_rank"] = rank
    return chosen


def _duplicates(records: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    counts = Counter(str(record.get(key) or "") for record in records)
    return {value: count for value, count in counts.items() if value and count > 1}


def build_quality_report(
    datasets: dict[str, list[dict[str, Any]]],
    quarantine: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    toilets = datasets["public_toilets"]
    accessibility = datasets["park_accessibility"]
    coord_collisions = Counter(
        (item["longitude"], item["latitude"]) for item in accessibility
    )
    return {
        "dataset_version": DATASET_VERSION,
        "counts": {key: len(value) for key, value in datasets.items()},
        "quarantine_count": len(quarantine),
        "quarantine_by_source_reason": dict(sorted(Counter(
            f"{item['source']}:{item['reason']}" for item in quarantine
        ).items())),
        "park_accessibility": {
            "facility_rows": sum(item["facility_row_count"] for item in accessibility),
            "duplicate_names": len(_duplicates(accessibility, "name")),
            "coordinate_collision_groups": sum(1 for count in coord_collisions.values() if count > 1),
        },
        "park_matching": {
            "accepted": sum(item["review_status"] == "accepted" for item in matches),
            "needs_review": sum(item["review_status"] == "needs_review" for item in matches),
            "by_method": dict(sorted(Counter(item["match_method"] for item in matches).items())),
            "first_batch_candidates": len(candidates),
            "candidate_districts": dict(sorted(Counter(item["district"] for item in candidates).items())),
        },
        "museums": {
            "blank_phone_count": sum(not item["phone"] for item in datasets["open_museums"]),
            "null_postcode_count": sum(item["postcode"] is None for item in datasets["open_museums"]),
        },
        "toilets": {
            "status": dict(sorted(Counter(item["source_status"] for item in toilets).items())),
            "coordinate_quality": dict(sorted(Counter(item["coordinate_quality"] for item in toilets).items())),
            "coordinate_parse_method": dict(sorted(Counter(
                item["coordinate_parse_method"] or "unparsed" for item in toilets
            ).items())),
            "non_revoked_count": sum(item["source_status_active"] for item in toilets),
            "publish_eligible_count": sum(item["publish_eligible"] for item in toilets),
            "duplicate_external_code_groups": len(_duplicates(toilets, "external_code")),
            "duplicate_name_groups": len(_duplicates(toilets, "name")),
        },
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as destination:
        for value in values:
            destination.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def build_beijing_staging(
    raw_dir: Path,
    output_root: Path,
    *,
    candidate_limit: int = 100,
    generated_at: str | None = None,
) -> dict[str, Any]:
    sources = locate_source_files(raw_dir)
    official, official_q = parse_official_parks(sources["official_parks"])
    accessibility, access_q = parse_accessibility_parks(sources["park_accessibility"])
    museums, museum_q = parse_museums(sources["open_museums"])
    toilets, toilet_q = parse_toilets(sources["public_toilets"])
    datasets = {
        "official_parks": official, "park_accessibility": accessibility,
        "open_museums": museums, "public_toilets": toilets,
    }
    quarantine = official_q + access_q + museum_q + toilet_q
    matches = match_parks(official, accessibility)
    candidates = select_park_candidates(official, accessibility, matches, limit=candidate_limit)
    quality = build_quality_report(datasets, quarantine, matches, candidates)

    output_dir = output_root / DATASET_VERSION
    output_dir.mkdir(parents=True, exist_ok=True)
    file_map = {
        "official_parks": "official_parks.jsonl",
        "park_accessibility": "park_accessibility_places.jsonl",
        "open_museums": "museums.jsonl",
        "public_toilets": "toilets.jsonl",
    }
    for key, filename in file_map.items():
        _write_jsonl(output_dir / filename, datasets[key])
    _write_jsonl(output_dir / "quarantine.jsonl", quarantine)
    _write_jsonl(output_dir / "park_matches.jsonl", matches)
    _write_json(output_dir / "park_candidates_100.json", candidates)
    _write_json(output_dir / "quality_report.json", quality)

    manifest_sources = {}
    for source_key, path in sources.items():
        manifest_sources[source_key] = {
            **SOURCE_METADATA[source_key], "filename": path.name,
            "bytes": path.stat().st_size, "sha256": sha256_file(path),
        }
    manifest = {
        "dataset_version": DATASET_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "sources": manifest_sources,
        "outputs": {
            **file_map, "quarantine": "quarantine.jsonl", "park_matches": "park_matches.jsonl",
            "park_candidates": "park_candidates_100.json", "quality_report": "quality_report.json",
        },
        "quality_summary": quality,
    }
    _write_json(output_dir / "manifest.json", manifest)
    return {"output_dir": str(output_dir), "manifest": manifest}

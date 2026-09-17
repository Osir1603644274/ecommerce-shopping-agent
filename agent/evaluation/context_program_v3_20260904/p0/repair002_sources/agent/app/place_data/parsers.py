from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .coordinates import parse_bd09_pair, parse_toilet_coordinate


PARK_HEADERS = ["序号", "所属区/单位", "公园名称", "类型", "级别", "地址", "咨询服务电话", "主管单位"]
ACCESSIBILITY_HEADERS = [
    "所属领域", "类别", "点位名称", "地址", "百度地图经纬度", "所属区划",
    "所属街道", "点位编码", "元素名称", "元素状态", "元素对应标识", "标识状态",
]
TOILET_COLUMNS = [
    "序号", "厕所名称", "所在区县", "所在街道乡镇", "作业保洁单位", "厕所编号",
    "地址（GPS信息）", "经度", "纬度", "建设方式", "类别", "农村公厕", "冲洗方式",
    "除臭方式", "指标状况", "男坑位", "女坑位", "无性别", "第三卫生间",
    "有无障碍设施", "无障碍厕所间", "全部实现无障碍公厕", "无障碍厕位",
    "管理间", "盥洗室", "粪便处理方式", "备注",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _read_csv(path: Path, encoding: str) -> list[list[str]]:
    with path.open("r", encoding=encoding, newline="") as source:
        return [row for row in csv.reader(source)]


def parse_official_parks(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - depends on optional runtime
        raise RuntimeError("PDF parsing requires the 'data' optional dependency") from exc

    records: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    with pdfplumber.open(path) as document:
        for page_number, page in enumerate(document.pages, start=1):
            tables = page.extract_tables()
            if len(tables) != 1:
                quarantine.append({
                    "source": "official_parks", "row_number": f"page:{page_number}",
                    "reason": "unexpected_table_count", "raw": {"table_count": len(tables)},
                })
                continue
            table = tables[0]
            if not table or [_clean(cell) for cell in table[0]] != PARK_HEADERS:
                quarantine.append({
                    "source": "official_parks", "row_number": f"page:{page_number}",
                    "reason": "unexpected_header", "raw": table[0] if table else [],
                })
                continue
            for page_row, raw_row in enumerate(table[1:], start=2):
                row = [_clean(cell) for cell in raw_row]
                if len(row) != 8 or not row[0].isdigit():
                    quarantine.append({
                        "source": "official_parks", "row_number": f"page:{page_number}:row:{page_row}",
                        "reason": "invalid_park_row", "raw": row,
                    })
                    continue
                records.append({
                    "source": "official_parks", "source_id": row[0], "name": row[2],
                    "place_kind": "park", "district": row[1], "address": row[5],
                    "park_type": row[3], "park_level": row[4], "phone": row[6],
                    "governing_unit": row[7], "source_page": page_number,
                })
    expected_ids = list(range(1, 1101))
    actual_ids = [int(record["source_id"]) for record in records]
    if actual_ids != expected_ids:
        quarantine.append({
            "source": "official_parks", "row_number": "dataset",
            "reason": "park_sequence_not_1_to_1100",
            "raw": {"count": len(actual_ids), "first": actual_ids[:3], "last": actual_ids[-3:]},
        })
    return records, quarantine


def parse_accessibility_parks(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _read_csv(path, "gb18030")
    if not rows or rows[0] != ACCESSIBILITY_HEADERS:
        raise ValueError(f"unexpected accessibility CSV header in {path}")
    grouped: dict[str, list[tuple[int, list[str]]]] = defaultdict(list)
    quarantine: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows[1:], start=2):
        if len(row) != len(ACCESSIBILITY_HEADERS):
            quarantine.append({
                "source": "park_accessibility", "row_number": row_number,
                "reason": "unexpected_column_count", "raw": row,
            })
            continue
        grouped[row[7].strip()].append((row_number, row))

    records: list[dict[str, Any]] = []
    for source_id, items in sorted(grouped.items()):
        first_number, first = items[0]
        identity = tuple(_clean(first[index]) for index in (2, 3, 4, 5, 6))
        if not source_id or any(tuple(_clean(row[index]) for index in (2, 3, 4, 5, 6)) != identity for _, row in items):
            quarantine.append({
                "source": "park_accessibility", "row_number": first_number,
                "reason": "inconsistent_rows_for_point_code", "raw": [row for _, row in items],
            })
            continue
        try:
            lon, lat = parse_bd09_pair(first[4])
        except ValueError as exc:
            quarantine.append({
                "source": "park_accessibility", "row_number": first_number,
                "reason": "invalid_bd09_coordinate", "detail": str(exc), "raw": first,
            })
            continue
        facilities = [
            {
                "name": _clean(row[8]), "status": _clean(row[9]),
                "signage": _clean(row[10]), "signage_status": _clean(row[11]),
            }
            for _, row in items
        ]
        records.append({
            "source": "park_accessibility", "source_id": source_id, "name": _clean(first[2]),
            "place_kind": "park", "district": _clean(first[5]), "street": _clean(first[6]),
            "address": _clean(first[3]), "longitude": lon, "latitude": lat,
            "coordinate_system": "BD-09", "coordinate_quality": "source_decimal",
            "facilities": facilities, "facility_row_count": len(items),
        })
    return records, quarantine


def parse_museums(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _read_csv(path, "utf-8-sig")
    if len(rows) < 2 or rows[1][:5] != ["序号", "博物馆名称", "通讯地址", "邮编", "电话"]:
        raise ValueError(f"unexpected museum CSV header in {path}")
    records: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    for row_number, raw_row in enumerate(rows[2:], start=3):
        row = [_clean(value) for value in raw_row]
        if len(row) >= 5 and row[0].isdigit() and 1 <= int(row[0]) <= 190:
            records.append({
                "source": "open_museums", "source_id": row[0], "name": row[1],
                "place_kind": "museum", "address": row[2],
                "postcode": None if row[3] in {"", "暂无", "/"} else row[3],
                "postcode_raw": row[3], "phone": row[4] or None,
            })
        elif any(row):
            quarantine.append({
                "source": "open_museums", "row_number": row_number,
                "reason": "orphan_or_malformed_row", "raw": row,
            })
    actual_ids = [int(record["source_id"]) for record in records]
    if actual_ids != list(range(1, 191)):
        quarantine.append({
            "source": "open_museums", "row_number": "dataset",
            "reason": "museum_sequence_not_1_to_190", "raw": {"count": len(records)},
        })
    return records, quarantine


def _optional_int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value.isdigit() else None


def parse_toilets(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _read_csv(path, "gb18030")
    records: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    for row_number, raw_row in enumerate(rows[5:], start=6):
        if not any(value.strip() for value in raw_row):
            continue
        if len(raw_row) != 27:
            quarantine.append({
                "source": "public_toilets", "row_number": row_number,
                "reason": "unexpected_column_count", "raw": raw_row,
            })
            continue
        row = dict(zip(TOILET_COLUMNS, (_clean(value) for value in raw_row), strict=True))
        if not row["序号"].isdigit():
            # The final source note/footer is retained as a quarantined non-record.
            quarantine.append({
                "source": "public_toilets", "row_number": row_number,
                "reason": "footer_or_non_data_row", "raw": raw_row,
            })
            continue
        coordinate = parse_toilet_coordinate(row["经度"], row["纬度"])
        external_code = row["厕所编号"]
        if external_code in {"", "/", "-", "无", "暂无", "没有", "是", "否"}:
            external_code = None
        records.append({
            "source": "public_toilets", "source_id": row["序号"], "external_code": external_code,
            "name": row["厕所名称"], "place_kind": "public_toilet", "district": row["所在区县"],
            "street": row["所在街道乡镇"], "address": row["地址（GPS信息）"],
            "longitude_raw": row["经度"], "latitude_raw": row["纬度"],
            "coordinate_system": "unknown_beijing_source", **coordinate,
            "source_status": row["指标状况"], "source_status_active": row["指标状况"] != "撤销",
            "category": row["类别"], "construction_method": row["建设方式"],
            "accessible_facility": row["有无障碍设施"],
            "accessible_room": row["无障碍厕所间"], "accessible_stall": row["无障碍厕位"],
            "male_stalls": _optional_int(row["男坑位"]), "female_stalls": _optional_int(row["女坑位"]),
            "gender_neutral_stalls": _optional_int(row["无性别"]),
            "third_restroom": _optional_int(row["第三卫生间"]),
            "cleaning_unit": row["作业保洁单位"], "flush_method": row["冲洗方式"],
            "deodorization_method": row["除臭方式"], "waste_treatment": row["粪便处理方式"],
        })
    actual_ids = [int(record["source_id"]) for record in records]
    if actual_ids != list(range(1, 13157)):
        quarantine.append({
            "source": "public_toilets", "row_number": "dataset",
            "reason": "toilet_sequence_not_1_to_13156", "raw": {"count": len(records)},
        })
    code_counts = defaultdict(int)
    for record in records:
        if record["external_code"]:
            code_counts[record["external_code"]] += 1
    for record in records:
        record["external_code_conflict"] = bool(
            record["external_code"] and code_counts[record["external_code"]] > 1
        )
        record["publish_eligible"] = bool(
            record["source_status_active"]
            and record["coordinate_quality"] in {"high", "corrected_reversed_decimal"}
            and not record["external_code_conflict"]
        )
    return records, quarantine


def locate_source_files(raw_dir: Path) -> dict[str, Path]:
    files = list(raw_dir.iterdir()) if raw_dir.exists() else []
    selected: dict[str, Path] = {}
    for path in files:
        if path.suffix.lower() == ".pdf":
            selected["official_parks"] = path
        elif path.suffix.lower() == ".csv":
            size = path.stat().st_size
            prefix = path.read_bytes()[:3]
            if prefix == b"\xef\xbb\xbf":
                selected["open_museums"] = path
            elif size > 1_000_000:
                selected["public_toilets"] = path
            else:
                selected["park_accessibility"] = path
    missing = {"official_parks", "park_accessibility", "open_museums", "public_toilets"} - selected.keys()
    if missing:
        raise FileNotFoundError(f"missing source datasets in {raw_dir}: {sorted(missing)}")
    return selected

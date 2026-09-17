from __future__ import annotations

import csv

from app.place_data.coordinates import parse_bd09_pair, parse_toilet_coordinate
from app.place_data.parsers import parse_museums, parse_toilets
from app.place_data.pipeline import match_parks, select_park_candidates


def test_coordinate_parser_marks_decimal_reversed_and_invalid() -> None:
    assert parse_bd09_pair("116.4, 39.9") == (116.4, 39.9)
    assert parse_toilet_coordinate("116.4", "39.9")["coordinate_quality"] == "high"

    reversed_result = parse_toilet_coordinate("39.9", "116.4")
    assert reversed_result["coordinate_quality"] == "corrected_reversed_decimal"
    assert (reversed_result["longitude"], reversed_result["latitude"]) == (116.4, 39.9)

    assert parse_toilet_coordinate("0", "0")["coordinate_quality"] == "out_of_bounds"
    assert parse_toilet_coordinate("/", "/")["coordinate_quality"] == "invalid"


def test_coordinate_parser_handles_dms_and_low_precision_suffix() -> None:
    dms = parse_toilet_coordinate("116°24′0″", "39°54′0″")
    assert dms["coordinate_parse_method"] == "dms"
    assert dms["coordinate_quality"] == "high"
    assert (dms["longitude"], dms["latitude"]) == (116.4, 39.9)

    suffix = parse_toilet_coordinate("116°4000000", "39°9000000")
    assert suffix["coordinate_parse_method"] == "decimal_suffix"
    assert suffix["coordinate_quality"] == "low"


def test_museum_parser_quarantines_orphan_phone(tmp_path) -> None:
    path = tmp_path / "museums.csv"
    rows = [
        ["title", "", "", "", ""],
        ["序号", "博物馆名称", "通讯地址", "邮编", "电话"],
    ]
    rows.extend([[str(i), f"museum-{i}", f"address-{i}", "100000", ""] for i in range(1, 191)])
    rows.insert(120, ["67010137"])
    with path.open("w", encoding="utf-8-sig", newline="") as destination:
        csv.writer(destination).writerows(rows)

    records, quarantine = parse_museums(path)

    assert len(records) == 190
    assert [item for item in quarantine if item["reason"] == "orphan_or_malformed_row"]


def test_toilet_parser_uses_logical_csv_rows_with_embedded_newline(tmp_path) -> None:
    path = tmp_path / "toilets.csv"
    rows = [[""] * 27 for _ in range(5)]
    data = [""] * 27
    data[0:10] = ["1", "测试公厕", "海淀区", "街道", "保洁单位", "/", "地址\n第二行", "116.4", "39.9", "/"]
    data[14] = "现有"
    rows.append(data)
    with path.open("w", encoding="gb18030", newline="") as destination:
        csv.writer(destination).writerows(rows)

    records, quarantine = parse_toilets(path)

    assert len(records) == 1
    assert records[0]["address"] == "地址 第二行"
    assert records[0]["external_code"] is None
    assert records[0]["publish_eligible"] is True
    assert any(item["reason"] == "toilet_sequence_not_1_to_13156" for item in quarantine)


def test_duplicate_accessibility_records_do_not_auto_merge_to_one_official() -> None:
    official = [{
        "source_id": "1", "name": "测试公园", "district": "海淀区", "address": "海淀区一号",
        "park_level": "一级", "park_type": "综合公园", "phone": "", "governing_unit": "",
    }]
    access = [
        {
            "source_id": source_id, "name": "测试公园", "district": "海淀区", "address": address,
            "longitude": lon, "latitude": 39.9, "facility_row_count": 6, "facilities": [],
        }
        for source_id, address, lon in (("a", "地址一", 116.4), ("b", "地址二", 116.5))
    ]

    matches = match_parks(official, access)

    assert all(item["review_status"] == "needs_review" for item in matches)
    assert not select_park_candidates(official, access, matches)

from __future__ import annotations

import pytest

from app.place_data import PlaceCatalog, evaluate_place_catalog


@pytest.fixture(scope="module")
def catalog() -> PlaceCatalog:
    return PlaceCatalog.load()


def test_runtime_snapshot_has_reviewed_scope_and_provenance(catalog: PlaceCatalog) -> None:
    assert catalog.size == 290
    assert catalog.metadata["catalogVersion"] == "beijing-places-v2"
    assert catalog.metadata["scope"] == "demo_snapshot_not_realtime"
    assert catalog.metadata["coordinateSystems"] == {"park": "BD-09", "museum": None}
    assert catalog.metadata["recordCountsByKind"] == {"park": 100, "museum": 190}
    assert set(catalog.metadata["sources"]) == {
        "official_parks",
        "park_accessibility",
        "open_museums",
    }


def test_exact_and_partial_name_search_are_deterministic(catalog: PlaceCatalog) -> None:
    exact = catalog.search(query="北京世界公园", kind="park")
    partial = catalog.search(query="世界公园", kind="park")

    assert exact.total == 1
    assert exact.items[0].id == "beijing-park-433"
    assert [item.id for item in partial.items] == ["beijing-park-433"]


def test_structured_filters_return_only_matching_records(catalog: PlaceCatalog) -> None:
    result = catalog.search(
        district="海淀区",
        park_type="综合公园",
        park_level="一级",
        limit=50,
    )

    assert result.total == 7
    assert all(item.district == "海淀区" for item in result.items)
    assert all(item.park_type == "综合公园" for item in result.items)
    assert all(item.park_level == "一级" for item in result.items)


def test_facility_filter_requires_explicit_source_status(catalog: PlaceCatalog) -> None:
    result = catalog.search(
        district="朝阳区",
        facility_name="无障碍厕所/厕位",
        facility_status="标准",
        limit=50,
    )

    assert result.total == 8
    assert all(
        any(facility.matches("无障碍厕所/厕位", "标准") for facility in item.facilities)
        for item in result.items
    )
    with pytest.raises(ValueError, match="require facility_name"):
        catalog.search(facility_status="标准")
    with pytest.raises(ValueError, match="require facility_name"):
        catalog.search(signage_status="标准")


def test_search_boundary_validation(catalog: PlaceCatalog) -> None:
    assert catalog.search(district="平谷区", kind="park").total == 0
    assert catalog.search(kind="museum", limit=50).total == 190
    with pytest.raises(ValueError, match="limit must be between 1 and 50"):
        catalog.search(limit=0)


def test_detail_keeps_coordinate_system_and_source_refs(catalog: PlaceCatalog) -> None:
    record = catalog.get("beijing-park-433")

    assert record is not None
    detail = record.to_detail()
    assert detail["name"] == "北京世界公园"
    assert detail["coordinateSystem"] == "BD-09"
    assert detail["sourceRefs"] == {
        "official_parks": "433",
        "park_accessibility": "ggfwcs-22-0206313",
    }
    assert catalog.get("beijing-park-missing") is None


def test_museum_search_and_detail_use_the_same_place_contract(catalog: PlaceCatalog) -> None:
    result = catalog.search(query="故宫博物院", kind="museum")

    assert result.total == 1
    assert result.items[0].id == "beijing-museum-1"
    detail = result.items[0].to_detail()
    assert detail["district"] == "东城区"
    assert detail["address"] == "北京市东城区景山前街4号"
    assert detail["phone"] == "65132255"
    assert detail["postcode"] == "100009"
    assert detail["coordinateSystem"] is None


def test_frozen_evaluation_suite_passes(catalog: PlaceCatalog) -> None:
    report = evaluate_place_catalog(catalog)

    assert report["totalCases"] == 12
    assert report["passedCases"] == 12
    assert report["passRate"] == 1.0

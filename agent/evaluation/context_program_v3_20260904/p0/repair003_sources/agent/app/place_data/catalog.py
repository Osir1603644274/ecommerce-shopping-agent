from __future__ import annotations

import copy
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CATALOG_VERSION = "beijing-places-v2"
DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "beijing_places_v2.json"
)
_LEVEL_ORDER = {"一级": 1, "二级": 2, "三级": 3, "四级": 4}


def _normalize(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    return re.sub(r"[\s·•—－_，,。.;；:：、/\\（）()\[\]【】《》<>\-]", "", normalized)


@dataclass(frozen=True)
class FacilityRecord:
    name: str
    status: str
    signage: str
    signage_status: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FacilityRecord":
        return cls(
            name=str(value["name"]),
            status=str(value["status"]),
            signage=str(value["signage"]),
            signage_status=str(value["signageStatus"]),
        )

    def matches(
        self,
        name: str | None,
        status: str | None,
        signage_status: str | None = None,
    ) -> bool:
        if name and _normalize(name) not in _normalize(self.name):
            return False
        if status and _normalize(status) != _normalize(self.status):
            return False
        if signage_status and _normalize(signage_status) != _normalize(self.signage_status):
            return False
        return True

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "signage": self.signage,
            "signageStatus": self.signage_status,
        }


@dataclass(frozen=True)
class PlaceRecord:
    id: str
    kind: str
    name: str
    district: str | None
    address: str
    longitude: float | None
    latitude: float | None
    coordinate_system: str | None
    park_type: str | None
    park_level: str | None
    postcode: str | None
    phone: str | None
    governing_unit: str | None
    facilities: tuple[FacilityRecord, ...]
    candidate_rank: int
    candidate_score: float
    match_method: str
    match_score: float
    quality_flags: tuple[str, ...]
    source_refs: dict[str, str]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlaceRecord":
        location = value.get("location") or {}
        park = value.get("park") or {}
        museum = value.get("museum") or {}
        contact = value["contact"]
        quality = value["quality"]
        return cls(
            id=str(value["id"]),
            kind=str(value["kind"]),
            name=str(value["name"]),
            district=str(value["district"]) if value.get("district") else None,
            address=str(value["address"]),
            longitude=float(location["longitude"]) if location.get("longitude") is not None else None,
            latitude=float(location["latitude"]) if location.get("latitude") is not None else None,
            coordinate_system=(
                str(location["coordinateSystem"])
                if location.get("coordinateSystem")
                else None
            ),
            park_type=str(park["type"]) if park.get("type") else None,
            park_level=str(park["level"]) if park.get("level") else None,
            postcode=str(museum["postcode"]) if museum.get("postcode") else None,
            phone=contact.get("phone"),
            governing_unit=contact.get("governingUnit"),
            facilities=tuple(FacilityRecord.from_dict(item) for item in value["facilities"]),
            candidate_rank=int(quality["candidateRank"]),
            candidate_score=float(quality["candidateScore"]),
            match_method=str(quality["matchMethod"]),
            match_score=float(quality["matchScore"]),
            quality_flags=tuple(str(item) for item in quality["flags"]),
            source_refs={str(key): str(item) for key, item in value["sourceRefs"].items()},
        )

    def search_text(self) -> str:
        facility_text = " ".join(
            f"{item.name} {item.status} {item.signage} {item.signage_status}"
            for item in self.facilities
        )
        return " ".join(
            str(value)
            for value in (
                self.name,
                self.district,
                self.address,
                self.park_type,
                self.park_level,
                self.postcode,
                facility_text,
            )
            if value
        )

    def to_search_item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "district": self.district,
            "address": self.address,
            "parkType": self.park_type,
            "parkLevel": self.park_level,
            "longitude": self.longitude,
            "latitude": self.latitude,
            "coordinateSystem": self.coordinate_system,
            "candidateRank": self.candidate_rank,
        }

    def to_detail(self) -> dict[str, Any]:
        return {
            **self.to_search_item(),
            "phone": self.phone,
            "governingUnit": self.governing_unit,
            "postcode": self.postcode,
            "facilities": [item.to_dict() for item in self.facilities],
            "quality": {
                "candidateScore": self.candidate_score,
                "matchMethod": self.match_method,
                "matchScore": self.match_score,
                "flags": list(self.quality_flags),
            },
            "sourceRefs": copy.deepcopy(self.source_refs),
        }


@dataclass(frozen=True)
class PlaceSearchResult:
    query: str | None
    filters: dict[str, Any]
    total: int
    items: tuple[PlaceRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "filters": copy.deepcopy(self.filters),
            "total": self.total,
            "count": len(self.items),
            "items": [item.to_search_item() for item in self.items],
        }


class PlaceCatalog:
    def __init__(self, records: list[PlaceRecord], metadata: dict[str, Any]):
        if not records:
            raise ValueError("place catalog must contain records")
        ids = [record.id for record in records]
        if len(ids) != len(set(ids)):
            raise ValueError("place catalog contains duplicate IDs")
        if any(
            record.kind == "park" and record.coordinate_system != "BD-09"
            for record in records
        ):
            raise ValueError("beijing park catalog must use BD-09 coordinates")
        self._records = tuple(records)
        self._by_id = {record.id: record for record in records}
        self.metadata = copy.deepcopy(metadata)

    @classmethod
    def load(cls, path: Path | None = None) -> "PlaceCatalog":
        catalog_path = path or DEFAULT_CATALOG_PATH
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        if payload.get("catalogVersion") != CATALOG_VERSION:
            raise ValueError(f"unsupported place catalog version: {payload.get('catalogVersion')!r}")
        records = [PlaceRecord.from_dict(item) for item in payload.get("places", [])]
        if payload.get("recordCount") != len(records):
            raise ValueError("place catalog recordCount does not match places")
        metadata = {key: value for key, value in payload.items() if key != "places"}
        return cls(records, metadata)

    @property
    def size(self) -> int:
        return len(self._records)

    def get(self, place_id: str) -> PlaceRecord | None:
        return self._by_id.get(place_id.strip())

    def find_name_mentions(self, text: str) -> tuple[PlaceRecord, ...]:
        """Return catalog records whose full or parenthesis-free name occurs in text."""

        text_key = _normalize(text)
        matches = []
        for record in self._records:
            name_key = _normalize(record.name)
            base_name_key = _normalize(re.split(r"[（(]", record.name, maxsplit=1)[0])
            if name_key in text_key or (len(base_name_key) >= 3 and base_name_key in text_key):
                matches.append(record)
        return tuple(matches)

    def search(
        self,
        *,
        query: str | None = None,
        district: str | None = None,
        kind: str | None = None,
        park_type: str | None = None,
        park_level: str | None = None,
        facility_name: str | None = None,
        facility_status: str | None = None,
        signage_status: str | None = None,
        limit: int = 10,
    ) -> PlaceSearchResult:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        normalized_query = query.strip() if query and query.strip() else None
        if (facility_status or signage_status) and not facility_name:
            raise ValueError("facility_status and signage_status require facility_name")

        filters = {
            key: value
            for key, value in {
                "district": district,
                "kind": kind,
                "parkType": park_type,
                "parkLevel": park_level,
                "facilityName": facility_name,
                "facilityStatus": facility_status,
                "signageStatus": signage_status,
            }.items()
            if value is not None
        }
        query_key = _normalize(normalized_query)
        scored: list[tuple[int, PlaceRecord]] = []
        for record in self._records:
            if district and _normalize(record.district) != _normalize(district):
                continue
            if kind and _normalize(record.kind) != _normalize(kind):
                continue
            if park_type and _normalize(record.park_type) != _normalize(park_type):
                continue
            if park_level and _normalize(record.park_level) != _normalize(park_level):
                continue
            if facility_name and not any(
                facility.matches(facility_name, facility_status, signage_status)
                for facility in record.facilities
            ):
                continue

            score = 0
            if query_key:
                name_key = _normalize(record.name)
                base_name_key = _normalize(re.split(r"[（(]", record.name, maxsplit=1)[0])
                search_key = _normalize(record.search_text())
                if query_key == name_key:
                    score = 100
                elif name_key and name_key in query_key:
                    score = 90
                elif base_name_key and base_name_key in query_key:
                    score = 85
                elif query_key in name_key:
                    score = 70
                elif query_key in search_key:
                    score = 30
                else:
                    continue
            scored.append((score, record))

        scored.sort(
            key=lambda item: (
                -item[0],
                _LEVEL_ORDER.get(item[1].park_level, 99),
                item[1].candidate_rank,
                item[1].id,
            )
        )
        return PlaceSearchResult(
            query=normalized_query,
            filters=filters,
            total=len(scored),
            items=tuple(record for _, record in scored[:limit]),
        )

import json
from pathlib import Path
from typing import Any

from .models import KnowledgeChunk


MERCHANT_DOC_SOURCE_NAME = "merchant_docs"
MERCHANT_DOC_SOURCE_TYPE = "merchant_doc"
DEFAULT_MERCHANT_DOCS_PATH = (
    Path(__file__).resolve().parents[2]
    / "knowledge_data"
    / "raw"
    / "merchant_docs"
    / "yelp_merchant_docs.json"
)


def merchant_doc_to_chunk(doc: dict[str, Any]) -> KnowledgeChunk:
    """Convert one Beijing-localized, Yelp-derived merchant doc into a chunk."""

    source_id = str(doc["sourceBusinessId"])
    metadata_keys = [
        "shopId",
        "sourceBusinessId",
        "name",
        "typeId",
        "typeName",
        "address",
        "city",
        "state",
        "postalCode",
        "longitude",
        "latitude",
        "coordinateSystem",
        "district",
        "anchorPlaceId",
        "anchorPlaceName",
        "projectionDistanceMeters",
        "projectionBearingDegrees",
        "projectionMethod",
        "dataNature",
        "realWorldNavigationSupported",
        "mappingExplanation",
        "reviewEvidenceScope",
        "localizationVersion",
        "originalName",
        "originalAddress",
        "sourceCity",
        "sourceState",
        "sourcePostalCode",
        "stars",
        "reviewCount",
        "isOpen",
        "categories",
        "attributes",
        "hours",
    ]
    metadata = {
        key: doc[key]
        for key in metadata_keys
        if key in doc and doc[key] not in (None, "", [], {})
    }
    original_name = str(doc.get("originalName") or "").strip()
    title = f"{doc['name']} 商户资料"
    if original_name and original_name != doc["name"]:
        title = f"{title}（Yelp原名：{original_name}）"

    return KnowledgeChunk(
        chunk_id=f"{MERCHANT_DOC_SOURCE_TYPE}:{source_id}:profile",
        source_type=MERCHANT_DOC_SOURCE_TYPE,
        source_id=source_id,
        chunk_index=1,
        source_version=str(doc.get("sourceVersion") or doc.get("updatedAt") or "1"),
        title=title,
        content=doc["content"],
        metadata=metadata,
        visibility=doc.get("visibility", "public"),
        language=doc.get("language", "zh"),
        tags=[
            tag
            for tag in (
                "merchant",
                str(doc.get("typeName", "")).strip(),
                *[str(category) for category in doc.get("categories", [])[:5]],
            )
            if tag
        ],
        updated_at=doc.get("updatedAt"),
    )


def load_merchant_docs(path: Path = DEFAULT_MERCHANT_DOCS_PATH) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    docs = payload.get("docs", [])
    if not isinstance(docs, list):
        raise ValueError("merchant docs payload must contain a docs list")
    return docs


def load_merchant_doc_chunks(path: Path = DEFAULT_MERCHANT_DOCS_PATH) -> list[KnowledgeChunk]:
    return [merchant_doc_to_chunk(doc) for doc in load_merchant_docs(path)]

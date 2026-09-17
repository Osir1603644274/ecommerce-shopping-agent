"""Isolated recommendation contracts; no model, service, or production imports.

The caller supplies the trusted current scope and catalog at validation time.
These contracts preserve source semantics; they cannot establish source truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _optional_text(value: object, name: str) -> None:
    if value is not None:
        _text(value, name)


@dataclass(frozen=True, order=True)
class ProductKey:
    source: str
    item_id: str

    def __post_init__(self) -> None:
        _text(self.source, "source")
        _text(self.item_id, "item_id")


@dataclass(frozen=True)
class Provenance:
    source_ref: str
    record_id: str | None = None
    row_number: int | None = None
    adapter: str = "manual_explicit_v1"
    price_evidence: str | None = None

    def __post_init__(self) -> None:
        _text(self.source_ref, "source_ref")
        _text(self.adapter, "adapter")
        _optional_text(self.record_id, "record_id")
        _optional_text(self.price_evidence, "price_evidence")
        if self.row_number is not None and (
            type(self.row_number) is not int or self.row_number < 1
        ):
            raise ValueError("row_number must be a positive integer")


@dataclass(frozen=True)
class Product:
    key: ProductKey
    provenance: Provenance
    title: str | None = None
    category: str | None = None
    brand: str | None = None
    attributes: Mapping[str, str] = field(default_factory=dict)
    price_minor: int | None = None
    currency: str | None = None
    money_unit: str | None = None
    price_missing_reason: str | None = "not_provided"

    def __post_init__(self) -> None:
        if not isinstance(self.key, ProductKey) or not isinstance(self.provenance, Provenance):
            raise ValueError("Product requires a ProductKey and Provenance")
        for name in ("title", "category", "brand"):
            _optional_text(getattr(self, name), name)
        for key, value in self.attributes.items():
            _text(key, "attribute key")
            _text(value, "attribute value")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))
        if self.currency is not None and (
            not isinstance(self.currency, str) or len(self.currency) != 3
            or not self.currency.isascii() or not self.currency.isalpha()
            or not self.currency.isupper()
        ):
            raise ValueError("currency must be an explicit uppercase three-letter code")
        if self.price_minor is None:
            _text(self.price_missing_reason, "price_missing_reason")
            if self.money_unit is not None:
                raise ValueError("money_unit must be unknown when the amount is missing")
        else:
            if type(self.price_minor) is not int or self.price_minor < 0:
                raise ValueError("price_minor must be a nonnegative integer")
            if self.money_unit != "minor" or self.currency is None:
                raise ValueError("a known price requires currency and money_unit='minor'")
            if self.price_missing_reason is not None or not self.provenance.price_evidence:
                raise ValueError("a known price requires source evidence and no missing reason")


EVENT_KINDS = frozenset({"click", "review", "purchase", "view", "add_to_cart", "favorite"})
TIMESTAMP_KINDS = frozenset({"relative", "unix_seconds", "unix_milliseconds"})
SPLITS = frozenset({"train", "validation", "test", "unsplit"})


@dataclass(frozen=True)
class Interaction:
    user_namespace: str
    user_id: str
    product_key: ProductKey
    event_kind: str
    timestamp: int
    timestamp_kind: str
    split: str
    provenance: Provenance

    def __post_init__(self) -> None:
        _text(self.user_namespace, "user_namespace")
        _text(self.user_id, "user_id")
        if not isinstance(self.product_key, ProductKey):
            raise ValueError("product_key must be a ProductKey")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("interaction requires Provenance")
        if self.event_kind not in EVENT_KINDS:
            raise ValueError(f"unsupported event_kind: {self.event_kind}")
        if type(self.timestamp) is not int or self.timestamp < 0:
            raise ValueError("timestamp must be a nonnegative integer")
        if self.timestamp_kind not in TIMESTAMP_KINDS:
            raise ValueError(f"unsupported timestamp_kind: {self.timestamp_kind}")
        if self.split not in SPLITS:
            raise ValueError(f"unsupported split: {self.split}")


@dataclass(frozen=True)
class CandidateScope:
    catalog_source: str
    catalog_id: str
    task_revision: str
    allowed_product_keys: frozenset[ProductKey] | None = None

    def __post_init__(self) -> None:
        for name in ("catalog_source", "catalog_id", "task_revision"):
            _text(getattr(self, name), name)
        if self.allowed_product_keys is not None:
            keys = frozenset(self.allowed_product_keys)
            if any(not isinstance(k, ProductKey) or k.source != self.catalog_source for k in keys):
                raise ValueError("allowed keys must belong to catalog_source")
            object.__setattr__(self, "allowed_product_keys", keys)


@dataclass(frozen=True)
class RecommendationResult:
    scope: CandidateScope
    product_keys: tuple[ProductKey, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CandidateScope):
            raise ValueError("scope must be CandidateScope")
        keys = tuple(self.product_keys)
        if any(not isinstance(key, ProductKey) for key in keys):
            raise ValueError("result entries must be ProductKey values")
        object.__setattr__(self, "product_keys", keys)


def build_catalog(products: Iterable[Product]) -> dict[ProductKey, Product]:
    """Index by source-qualified ID. Equal titles never identify equal products."""
    catalog: dict[ProductKey, Product] = {}
    for product in products:
        if product.key in catalog:
            raise ValueError(f"duplicate catalog product key: {product.key}")
        catalog[product.key] = product
    return catalog


def validate_recommendation_result(
    result: RecommendationResult,
    expected_scope: CandidateScope,
    catalog: Mapping[ProductKey, Product],
) -> tuple[Product, ...]:
    """Reject stale/foreign/unknown/duplicate entries, returning hydrated products."""
    if result.scope != expected_scope:
        raise ValueError("result scope is stale or differs from the current catalog/task")
    seen: set[ProductKey] = set()
    products = []
    for key in result.product_keys:
        if key.source != expected_scope.catalog_source:
            raise ValueError(f"cross-catalog product: {key}")
        if key in seen:
            raise ValueError(f"duplicate result product key: {key}")
        seen.add(key)
        if expected_scope.allowed_product_keys is not None and key not in expected_scope.allowed_product_keys:
            raise ValueError(f"product outside candidate scope: {key}")
        if key not in catalog:
            raise ValueError(f"unknown product ID: {key}")
        product = catalog[key]
        if product.key != key:
            raise ValueError("catalog index key disagrees with product identity")
        products.append(product)
    return tuple(products)


def select_history(
    events: Iterable[Interaction],
    target: Interaction,
    allowed_splits: frozenset[str] = frozenset({"train"}),
) -> tuple[Interaction, ...]:
    """Strictly earlier same-user events. Equal timestamps cannot leak into history.

    Relative timestamps must share one documented source clock within a user
    namespace. Different timestamp units cannot be compared implicitly.
    Event kind is preserved: review evidence remains review evidence.
    """
    if not allowed_splits or not set(allowed_splits).issubset(SPLITS):
        raise ValueError("allowed_splits must explicitly select supported splits")
    history = []
    for event in events:
        if (event.user_namespace, event.user_id) != (target.user_namespace, target.user_id):
            continue
        if event.split not in allowed_splits:
            continue
        if event.timestamp_kind != target.timestamp_kind:
            raise ValueError("incomparable timestamp kinds for the same user")
        if event.timestamp < target.timestamp:
            history.append(event)
    return tuple(sorted(history, key=lambda event: event.timestamp))


def adapt_kuaisearch_document(
    row: Mapping[str, object], *, source: str, source_ref: str,
    row_number: int | None = None,
) -> Product:
    """Adapter for the observed local documents.jsonl schema only.

    Verified fields: doc_id/title/brand/attr_value/seller_name. This is not
    an adapter for raw KuaiSearch behavior files or the used-phone catalog.
    The source schema supplies neither price nor currency; neither is guessed.
    """
    item_id = row.get("doc_id")
    _text(item_id, "doc_id")
    attributes = {}
    for key in ("attr_value", "seller_name"):
        value = row.get(key)
        if value is not None and value != "":
            _text(value, key)
            attributes[key] = value
    return Product(
        key=ProductKey(source, item_id),
        provenance=Provenance(source_ref, item_id, row_number, "local_kuaisearch_document_v1"),
        title=None if row.get("title") in (None, "") else row["title"],
        brand=None if row.get("brand") in (None, "") else row["brand"],
        attributes=attributes,
        price_missing_reason="not_in_source_schema",
    )


def adapt_kuaisearch_lite_item(
    row: Mapping[str, object], *, source_ref: str, source: str = "kuaisearch",
    row_number: int | None = None,
) -> Product:
    """Adapt verified items_lite.train.jsonl fields without synthetic prices.

    Raw item IDs are stringified, never prefixed with a production document ID.
    Category names form a display path; raw IDs/names (including UNKNOWN) remain
    available in attributes so the normalization can be audited.
    """
    raw_id = row.get("item_id")
    if type(raw_id) not in (int, str):
        raise ValueError("item_id must be an explicit integer or string")
    item_id = str(raw_id)
    _text(item_id, "item_id")
    attributes = {}
    field_names = ["brand_id", "brand_name", "seller_id", "seller_name"]
    field_names += [f"category_level{level}_{suffix}" for level in (1, 2, 3) for suffix in ("id", "name")]
    for name in field_names:
        value = row.get(name)
        if value is None or value == "":
            continue
        if type(value) not in (str, int):
            raise ValueError(f"{name} must be a string or integer")
        attributes[name] = str(value)
    categories = [attributes.get(f"category_level{level}_name") for level in (1, 2, 3)]
    category = " / ".join(name for name in categories if name and name != "UNKNOWN") or None
    brand = row.get("brand_name")
    title = row.get("item_title")
    return Product(
        key=ProductKey(source, item_id),
        provenance=Provenance(source_ref, item_id, row_number, "kuaisearch_lite_item_v1"),
        title=None if title in (None, "") else title,
        category=category,
        brand=None if brand in (None, "", "UNKNOWN") else brand,
        attributes=attributes,
        price_missing_reason="not_in_source_schema",
    )

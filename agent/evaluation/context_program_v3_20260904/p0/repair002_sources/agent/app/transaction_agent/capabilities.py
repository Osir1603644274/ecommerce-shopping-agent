"""Internal misuse guards for a future authenticated Transaction Agent.

These objects are not an authentication or authorization boundary.  Python
callers in this process can inspect private modules, so only a trusted request
auth provider may authorize real writes.  Until that provider is wired, the
transaction runtime rejects every preview/write before Redis or HTTP.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

SHOPPING_TOOL_NAMES = frozenset({
    "search_products", "get_product_details", "compare_products",
    "rerank_products_in_scope",
})
# These names describe an internal future handoff only.  They are neither
# public LLM schemas nor complete persisted Harness contracts.
TRANSACTION_PREVIEW_TOOL_NAMES = frozenset({
    "preview_order", "preview_cancel_order", "preview_payment",
})
TRANSACTION_READ_TOOL_NAMES = frozenset({"query_order_status"})
TRANSACTION_WRITE_TOOL_NAMES = frozenset({
    "create_order", "cancel_order", "create_payment",
})

_ISSUER = object()
_LIVE: dict[int, object] = {}
_CURRENT: ContextVar[_TransactionCapability | None] = ContextVar(
    "transaction_capability", default=None
)


class _TransactionCapability:
    __slots__ = ("_marker", "_sealed")

    def __new__(cls, *args: Any, _issuer: object | None = None, **kwargs: Any):
        if cls is not _TransactionCapability or _issuer is not _ISSUER:
            raise TypeError("internal transaction capability only")
        obj = super().__new__(cls)
        object.__setattr__(obj, "_marker", object())
        object.__setattr__(obj, "_sealed", True)
        _LIVE[id(obj)] = obj._marker
        return obj

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise TypeError("capability is frozen")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return "_TransactionCapability()"

    def __reduce_ex__(self, protocol: int) -> Any:
        raise TypeError("capability is not serialisable")

    def __copy__(self) -> Any:
        raise TypeError("capability cannot be copied")

    __deepcopy__ = __copy__

    def _valid(self) -> bool:
        return _LIVE.get(id(self)) is self._marker


def _issue_transaction_capability() -> _TransactionCapability:
    """Private future-provider hook; not an authentication claim."""
    return _TransactionCapability(_issuer=_ISSUER)


def _current_transaction_capability() -> _TransactionCapability | None:
    value = _CURRENT.get()
    return value if type(value) is _TransactionCapability and value._valid() else None


@contextmanager
def _bind_transaction_capability(capability: _TransactionCapability) -> Iterator[None]:
    if type(capability) is not _TransactionCapability or not capability._valid():
        raise PermissionError("invalid internal transaction capability")
    token = _CURRENT.set(capability)
    try:
        yield
    finally:
        _CURRENT.reset(token)


class _CapabilityDispatcher:
    """Internal name gate; it cannot authenticate a caller."""

    @staticmethod
    def allowed(tool_name: str, capability: object | None) -> bool:
        return (
            type(capability) is _TransactionCapability
            and capability._valid()
            and tool_name in (
                TRANSACTION_PREVIEW_TOOL_NAMES
                | TRANSACTION_READ_TOOL_NAMES
                | TRANSACTION_WRITE_TOOL_NAMES
            )
        )

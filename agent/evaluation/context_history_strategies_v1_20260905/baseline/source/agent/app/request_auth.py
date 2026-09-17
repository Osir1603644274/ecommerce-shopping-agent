"""Provider-neutral request-auth capability seam.

This module intentionally does *not* authenticate a header.  Production must
provide a trusted gateway/introspection adapter.  The in-process capability is
only a domain seam for that future adapter and cannot turn a raw bearer token,
session id, or request body into an authenticated context.
"""
from __future__ import annotations

import copy
import secrets
import weakref
from dataclasses import dataclass, field
from typing import Protocol

from .memory.projection_client import MemoryAccessCredential

PRODUCTION_AUTH_PROVIDER_UNAVAILABLE = "PRODUCTION_AUTH_PROVIDER_UNAVAILABLE"


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False, repr=False)
class RequestAuthContext:
    """An issued, non-serializable request-local bearer capability."""

    _credential: MemoryAccessCredential = field(repr=False)
    _nonce: bytes = field(repr=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(PRODUCTION_AUTH_PROVIDER_UNAVAILABLE)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("RequestAuthContext cannot be subclassed")

    def __copy__(self) -> "RequestAuthContext":
        raise TypeError("RequestAuthContext cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> "RequestAuthContext":
        raise TypeError("RequestAuthContext cannot be copied")

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("RequestAuthContext cannot be pickled")


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False, repr=False)
class TrustedRequestAuthIssuer:
    """Server-side issuer capability; direct construction is forbidden."""

    _nonce: bytes = field(repr=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(PRODUCTION_AUTH_PROVIDER_UNAVAILABLE)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("TrustedRequestAuthIssuer cannot be subclassed")

    def issue(self, credential: object) -> RequestAuthContext:
        """Issue only after the provider has independently authenticated it."""
        issued = _ISSUERS.get(id(self))
        if issued is None or issued.reference() is not self or issued.nonce != self._nonce:
            raise ValueError(PRODUCTION_AUTH_PROVIDER_UNAVAILABLE)
        if type(credential) is not MemoryAccessCredential:
            raise ValueError("credential must be an exact MemoryAccessCredential")
        return _issue_context(self, credential)

    def __copy__(self) -> "TrustedRequestAuthIssuer":
        raise TypeError("TrustedRequestAuthIssuer cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> "TrustedRequestAuthIssuer":
        raise TypeError("TrustedRequestAuthIssuer cannot be copied")

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("TrustedRequestAuthIssuer cannot be pickled")


class TrustedRequestAuthProvider(Protocol):
    """Production port: validate a request outside this domain module."""

    async def authenticate_current_request(self) -> RequestAuthContext | None:
        """Return an issued context, never an owner id or raw request fields."""


@dataclass(frozen=True, slots=True)
class _IssuedIssuer:
    reference: weakref.ReferenceType[TrustedRequestAuthIssuer]
    nonce: bytes


@dataclass(frozen=True, slots=True)
class _IssuedContext:
    reference: weakref.ReferenceType[RequestAuthContext]
    nonce: bytes
    issuer_nonce: bytes
    credential: MemoryAccessCredential


_ISSUERS: dict[int, _IssuedIssuer] = {}
_CONTEXTS: dict[int, _IssuedContext] = {}


def _new_issuer_for_verified_provider() -> TrustedRequestAuthIssuer:
    """Provider-neutral in-process seam, not a security boundary.

    The use of ``object.__new__`` here is intentionally only a composition
    hook for tests/wiring.  Python reflection cannot establish provider trust;
    real provider/runtime authentication remains HOLD and must be bound by a
    trusted external adapter.
    """
    issuer = object.__new__(TrustedRequestAuthIssuer)
    nonce = secrets.token_bytes(32)
    object.__setattr__(issuer, "_nonce", nonce)
    key = id(issuer)

    def cleanup(reference: weakref.ReferenceType[TrustedRequestAuthIssuer]) -> None:
        stored = _ISSUERS.get(key)
        if stored is not None and stored.reference is reference:
            _ISSUERS.pop(key, None)

    reference = weakref.ref(issuer, cleanup)
    _ISSUERS[key] = _IssuedIssuer(reference, nonce)
    return issuer


def _issue_context(
    issuer: TrustedRequestAuthIssuer,
    credential: MemoryAccessCredential,
) -> RequestAuthContext:
    context = object.__new__(RequestAuthContext)
    nonce = secrets.token_bytes(32)
    object.__setattr__(context, "_credential", credential)
    object.__setattr__(context, "_nonce", nonce)
    key = id(context)

    def cleanup(reference: weakref.ReferenceType[RequestAuthContext]) -> None:
        stored = _CONTEXTS.get(key)
        if stored is not None and stored.reference is reference:
            _CONTEXTS.pop(key, None)

    reference = weakref.ref(context, cleanup)
    _CONTEXTS[key] = _IssuedContext(reference, nonce, issuer._nonce, credential)
    return context


def _memory_credential(context: object) -> MemoryAccessCredential:
    """Internal-only adapter handoff; validates identity, nonce and credential."""
    if type(context) is not RequestAuthContext:
        raise ValueError(PRODUCTION_AUTH_PROVIDER_UNAVAILABLE)
    issued = _CONTEXTS.get(id(context))
    if (
        issued is None
        or issued.reference() is not context
        or issued.nonce != context._nonce
        or issued.credential is not context._credential
        or not any(
            item.reference() is not None and item.nonce == issued.issuer_nonce
            for item in _ISSUERS.values()
        )
    ):
        raise ValueError(PRODUCTION_AUTH_PROVIDER_UNAVAILABLE)
    return issued.credential


__all__ = [
    "PRODUCTION_AUTH_PROVIDER_UNAVAILABLE",
    "RequestAuthContext",
    "TrustedRequestAuthIssuer",
    "TrustedRequestAuthProvider",
]

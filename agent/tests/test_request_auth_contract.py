import asyncio
import copy
import pickle

import pytest

from app.memory.projection_client import MemoryAccessCredential
from app.memory.projection_client import _credential_token
from app.request_auth import (
    PRODUCTION_AUTH_PROVIDER_UNAVAILABLE,
    RequestAuthContext,
    TrustedRequestAuthIssuer,
    _memory_credential,
    _new_issuer_for_verified_provider,
)


def test_only_private_verified_issuer_can_mint_and_token_is_not_public():
    credential = MemoryAccessCredential("token-one")
    with pytest.raises(TypeError, match=PRODUCTION_AUTH_PROVIDER_UNAVAILABLE):
        RequestAuthContext(credential)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match=PRODUCTION_AUTH_PROVIDER_UNAVAILABLE):
        TrustedRequestAuthIssuer()  # type: ignore[call-arg]

    issuer = _new_issuer_for_verified_provider()
    with pytest.raises(ValueError):
        issuer.issue("Bearer token-one")
    context = issuer.issue(credential)
    assert _memory_credential(context) is credential
    assert "token-one" not in repr(context)
    assert not hasattr(context, "access_token")
    assert not hasattr(context, "owner_user_id")


def test_context_copy_pickle_subclass_and_object_new_forgery_fail_closed():
    issuer = _new_issuer_for_verified_provider()
    context = issuer.issue(MemoryAccessCredential("token-two"))
    with pytest.raises(TypeError):
        copy.copy(context)
    with pytest.raises(TypeError):
        copy.deepcopy(context)
    with pytest.raises(TypeError):
        pickle.dumps(context)
    with pytest.raises(TypeError):
        class ForgedContext(RequestAuthContext):
            pass

    forged = object.__new__(RequestAuthContext)
    object.__setattr__(forged, "_credential", MemoryAccessCredential("token-two"))
    object.__setattr__(forged, "_nonce", b"x" * 32)
    with pytest.raises(ValueError, match=PRODUCTION_AUTH_PROVIDER_UNAVAILABLE):
        _memory_credential(forged)


def test_raw_bearer_session_and_body_cannot_mint_context():
    issuer = _new_issuer_for_verified_provider()
    for untrusted in ("raw-bearer", "browser-session-1", {"ownerUserId": "u-1"}):
        with pytest.raises(ValueError):
            issuer.issue(untrusted)


def test_concurrent_issued_contexts_are_isolated_without_current_context_global():
    first = _new_issuer_for_verified_provider()
    second = _new_issuer_for_verified_provider()

    async def issue_and_read(issuer, token):
        context = issuer.issue(MemoryAccessCredential(token))
        await asyncio.sleep(0)
        return context, _memory_credential(context)

    async def run_both():
        return await asyncio.gather(
            issue_and_read(first, "token-first"),
            issue_and_read(second, "token-second"),
        )

    (first_context, first_credential), (second_context, second_credential) = asyncio.run(run_both())
    assert first_context is not second_context
    assert not hasattr(first_credential, "access_token")
    assert not hasattr(second_credential, "access_token")
    assert _credential_token(first_credential) == "token-first"
    assert _credential_token(second_credential) == "token-second"

from datetime import UTC, datetime, timedelta
import copy
import pickle
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domains.ecommerce.fulfillment import (
    BackendQueryResult,
    CompensationStatus,
    ConfirmationEvidence,
    ConfirmationIssuer,
    ErrorCode,
    FulfillmentAction,
    FulfillmentIntent,
    FulfillmentStatus,
    ImmutablePreviewBinding,
    InvalidFulfillmentTransition,
    OwnerPrincipal,
    PaymentCompensationPolicyError,
    QueryReceipt,
    QueryReceiptIssuer,
    SagaExecution,
    SagaReceipt,
    SagaStep,
    StepKind,
    StepStatus,
    command_idempotency_key,
)
from app.control.validation_contracts import harness_contract_tool_names


def setup(action=FulfillmentAction.ORDER_AND_PAY):
    owner = OwnerPrincipal.issue("user-1")
    intent = FulfillmentIntent(userId="user-1", taskId="task-1", executionId="exec-1", previewDigest="preview-1", action=action, amount=12, currency="CNY", orderedItemsDigest="items-1")
    execution = SagaExecution.from_intent(intent, owner=owner)
    evidence = ConfirmationIssuer.issue(owner, intent, confirmation_id="confirm-1")
    return owner, intent, execution.confirm(evidence, owner=owner)


def test_order_payment_fulfillment_chain_and_order_only_chain():
    owner, _, execution = setup()
    done = (execution.start_step(StepKind.ORDER, owner=owner)
            .record_step_success(StepKind.ORDER, backend_reference="order-1", owner=owner)
            .start_step(StepKind.PAYMENT, owner=owner)
            .record_step_success(StepKind.PAYMENT, backend_reference="payment-1", owner=owner)
            .start_step(StepKind.FULFILLMENT, owner=owner)
            .record_step_success(StepKind.FULFILLMENT, backend_reference="fulfill-1", owner=owner))
    assert done.status is FulfillmentStatus.COMPLETED
    assert done.receipt(StepKind.FULFILLMENT, owner=owner).backend_reference == "fulfill-1"

    owner, _, execution = setup(FulfillmentAction.ORDER_ONLY)
    done = (execution.start_step(StepKind.ORDER, owner=owner)
            .record_step_success(StepKind.ORDER, backend_reference="order-2", owner=owner)
            .start_step(StepKind.FULFILLMENT, owner=owner)
            .record_step_success(StepKind.FULFILLMENT, backend_reference="fulfill-2", owner=owner))
    assert done.status is FulfillmentStatus.COMPLETED
    with pytest.raises(InvalidFulfillmentTransition):
        done.start_step(StepKind.PAYMENT, owner=owner)


def test_confirmation_owner_binding_single_use_and_private_construction():
    owner, intent, execution = setup()
    with pytest.raises(PermissionError):
        execution.confirm(ConfirmationIssuer.issue(OwnerPrincipal.issue("other"), intent, confirmation_id="other"), owner=owner)
    evidence = ConfirmationIssuer.issue(owner, intent, confirmation_id="once")
    with pytest.raises(TypeError):
        ConfirmationEvidence.model_construct()
    with pytest.raises(TypeError):
        evidence.copy()
    with pytest.raises(TypeError):
        evidence.copy(update={"owner_user_id": "other"})
    with pytest.raises(ValueError):
        ConfirmationEvidence.model_validate(evidence.model_dump())
    other = SagaExecution.from_intent(intent, owner=owner)
    confirmed = other.confirm(evidence, owner=owner)
    with pytest.raises(PermissionError):
        confirmed.confirm(evidence, owner=owner)


def test_command_key_binds_every_dimension_and_strict_json():
    common = dict(owner_user_id="user-1", task_id="task-1", execution_id="exec-1", action=FulfillmentAction.ORDER_AND_PAY, confirmation_id="c", confirmation_digest="cd", step="order", preview_digest="p", amount=1, currency="CNY", ordered_items_digest="items", canonical_args={"b": 2, "a": 1})
    key = command_idempotency_key(**common)
    assert key == command_idempotency_key(**{**common, "canonical_args": {"a": 1, "b": 2}})
    for changed in ("user-2", "task-2", "exec-2", "c2", "cd2", "p2", "items2"):
        field = {"user-2": "owner_user_id", "task-2": "task_id", "exec-2": "execution_id", "c2": "confirmation_id", "cd2": "confirmation_digest", "p2": "preview_digest", "items2": "ordered_items_digest"}[changed]
        assert command_idempotency_key(**{**common, field: changed}) != key
    with pytest.raises(ValueError): command_idempotency_key(**common, session_id="sid")
    with pytest.raises(ValueError): command_idempotency_key(**{**common, "canonical_args": {"prompt": "x"}})
    with pytest.raises(ValueError): command_idempotency_key(**{**common, "action": "order_and_pay"})


def test_preview_rechecked_with_authority_clock_and_no_injected_now():
    owner, _, _ = setup()
    expiry = datetime.now(UTC) + timedelta(minutes=1)
    intent = FulfillmentIntent(userId="user-1", taskId="task-1", executionId="exec-1", previewDigest="preview-1", action=FulfillmentAction.ORDER_AND_PAY, amount=12, currency="CNY", orderedItemsDigest="items-1", expiresAt=expiry)
    binding = ImmutablePreviewBinding(userId="user-1", taskId="task-1", executionId="exec-1", previewDigest="preview-1", action=FulfillmentAction.ORDER_AND_PAY, amount=12, currency="CNY", orderedItemsDigest="items-1", expiresAt=expiry)
    binding.assert_matches(intent)
    with pytest.raises(TypeError): binding.assert_matches(intent, now=datetime.now(UTC))
    with pytest.raises(ValidationError): ImmutablePreviewBinding(userId="user-1", executionId="e", previewDigest="p", expiresAt=datetime.now() + timedelta(minutes=1))
    with pytest.raises(PermissionError): setup()[2].start_step(StepKind.ORDER, owner=OwnerPrincipal.issue("other"))


def test_unknown_consumes_authoritative_query_receipt_and_only_not_found_retries():
    owner, _, execution = setup()
    unknown = execution.start_step(StepKind.ORDER, owner=owner).mark_unknown(StepKind.ORDER, reason=ErrorCode.TIMEOUT, owner=owner)
    with pytest.raises(ValueError): QueryReceipt.model_validate({"aggregateId": "exec-1", "executionId": "exec-1", "step": "order", "commandIdempotencyKey": "x", "querySequence": 1, "result": "NOT_FOUND", "observedAt": datetime.now(UTC)})
    with pytest.raises(PermissionError): unknown.resolve_unknown(StepKind.ORDER, object(), owner=owner)
    receipt = QueryReceiptIssuer.issue(unknown, StepKind.ORDER, result=BackendQueryResult.NOT_FOUND)
    retried = unknown.resolve_unknown(StepKind.ORDER, receipt, owner=owner)
    assert retried.status is FulfillmentStatus.ORDER_CREATING
    assert retried.step(StepKind.ORDER).command_idempotency_key == unknown.step(StepKind.ORDER).command_idempotency_key
    with pytest.raises(InvalidFulfillmentTransition): retried.resolve_unknown(StepKind.ORDER, receipt, owner=owner)

    held = retried.mark_unknown(StepKind.ORDER, reason=ErrorCode.TIMEOUT, owner=owner)
    receipt = QueryReceiptIssuer.issue(held, StepKind.ORDER, result=BackendQueryResult.FOUND_NOT_COMMITTED)
    held = held.resolve_unknown(StepKind.ORDER, receipt, owner=owner)
    assert held.status is FulfillmentStatus.UNKNOWN
    assert held.step(StepKind.ORDER).status is StepStatus.UNKNOWN


def test_query_receipt_sequence_cross_aggregate_copy_and_pickle_fail():
    owner, _, execution = setup()
    unknown = execution.start_step(StepKind.ORDER, owner=owner).mark_unknown(StepKind.ORDER, reason=ErrorCode.TIMEOUT, owner=owner)
    receipt = QueryReceiptIssuer.issue(unknown, StepKind.ORDER, result=BackendQueryResult.COMMITTED, backend_reference="order-1")
    with pytest.raises(TypeError): receipt.model_copy()
    with pytest.raises(TypeError): receipt.copy()
    with pytest.raises(TypeError): receipt.copy(update={"execution_id": "other"})
    with pytest.raises(TypeError): pickle.dumps(receipt)
    with pytest.raises(InvalidFulfillmentTransition): unknown.resolve_unknown(StepKind.PAYMENT, receipt, owner=owner)
    with pytest.raises(TypeError): copy.copy(receipt)
    resolved = unknown.resolve_unknown(StepKind.ORDER, receipt, owner=owner)
    assert resolved.status is FulfillmentStatus.ORDER_CREATED


def test_payment_failure_cancel_and_payment_success_forbids_refund():
    owner, _, execution = setup()
    failed = (execution.start_step(StepKind.ORDER, owner=owner)
              .record_step_success(StepKind.ORDER, backend_reference="order-1", owner=owner)
              .start_step(StepKind.PAYMENT, owner=owner)
              .record_step_failure(StepKind.PAYMENT, error_code=ErrorCode.DECLINED, owner=owner))
    compensating = failed.request_cancel_after_payment_failure(owner=owner).start_step(StepKind.CANCEL_ORDER, owner=owner).record_step_success(StepKind.CANCEL_ORDER, backend_reference="cancel-1", owner=owner)
    assert compensating.status is FulfillmentStatus.COMPENSATED
    owner, _, execution = setup()
    paid = (execution.start_step(StepKind.ORDER, owner=owner)
            .record_step_success(StepKind.ORDER, backend_reference="order-2", owner=owner)
            .start_step(StepKind.PAYMENT, owner=owner)
            .record_step_success(StepKind.PAYMENT, backend_reference="payment-2", owner=owner))
    manual = paid.request_auto_refund(owner=owner)
    assert manual.status is FulfillmentStatus.MANUAL_REVIEW and manual.compensation_status is CompensationStatus.PROHIBITED


def test_status_receipt_step_invariants_sensitive_and_registry_boundary():
    with pytest.raises(ValidationError): SagaStep(step=StepKind.ORDER, userId="u", executionId="e", previewDigest="p", backendReference="diagnosis:abc")
    with pytest.raises(ValidationError): SagaStep(step=StepKind.ORDER, status=StepStatus.FAILED, userId="u", executionId="e", previewDigest="p", errorCode="free-form")
    owner, _, execution = setup()
    step_execution = execution.start_step(StepKind.ORDER, owner=owner)
    receipt = step_execution.receipt(StepKind.ORDER, owner=owner)
    assert not {"token", "prompt", "authorization", "sessionId"} & set(receipt.model_dump(by_alias=True))
    with pytest.raises(ValidationError): SagaReceipt.model_validate({**receipt.model_dump(), "token": "x"})
    with pytest.raises(TypeError): receipt.model_copy()
    with pytest.raises(TypeError): receipt.copy()
    exposed = step_execution.steps[0]
    object.__setattr__(exposed, "status", StepStatus.SUCCEEDED)
    assert step_execution.steps[0].status is StepStatus.CREATING
    assert {"create_order", "cancel_order", "create_payment"}.isdisjoint(harness_contract_tool_names())
    with pytest.raises((TypeError, ValidationError)): step_execution.model_copy(update={"status": FulfillmentStatus.COMPLETED})


def test_fulfillment_failed_requires_failed_step_and_backend_refs_are_opaque():
    owner, _, execution = setup()
    failed = execution.start_step(StepKind.ORDER, owner=owner).record_step_failure(StepKind.ORDER, error_code=ErrorCode.TIMEOUT, owner=owner)
    assert failed.status is FulfillmentStatus.FAILED
    for bad in ("diagnosis", "BearerX", "abc:def", "token-ref"):
        with pytest.raises((ValueError, ValidationError)): failed.start_step(StepKind.ORDER, owner=owner) if False else execution.start_step(StepKind.ORDER, owner=owner).record_step_success(StepKind.ORDER, backend_reference=bad, owner=owner)


def test_confirmation_and_execution_cannot_be_caller_constructed_or_owner_forged():
    owner, intent, execution = setup()
    with pytest.raises(ValidationError):
        SagaExecution(userId="user-1", executionId="forged", taskId="task-1", previewDigest="p", confirmed=True, confirmationId="c", confirmationDigest="d")
    creating = SagaStep(step=StepKind.ORDER, status=StepStatus.CREATING, userId="user-1", executionId="forged", taskId="task-1", previewDigest="p")
    with pytest.raises(ValidationError):
        SagaExecution(userId="user-1", executionId="forged", taskId="task-1", previewDigest="p", steps=(creating,))
    forged_owner = OwnerPrincipal.issue("user-1")
    object.__setattr__(forged_owner, "user_id", "other")
    with pytest.raises(PermissionError):
        execution.start_step(StepKind.ORDER, owner=forged_owner)


def test_execution_public_copy_update_and_identity_rebind_fail_closed():
    owner, _, execution = setup()
    for operation in (
        lambda: execution.model_copy(),
        lambda: execution.model_copy(update={"owner_user_id": "other"}),
        lambda: execution.clone(status=FulfillmentStatus.AWAITING_CONFIRMATION),
        lambda: SagaExecution.model_construct(
            userId="user-1", executionId="forged", previewDigest="p"
        ),
        lambda: copy.copy(execution),
        lambda: copy.deepcopy(execution),
        lambda: pickle.dumps(execution),
        lambda: execution.copy(),
        lambda: execution.copy(update={"owner_user_id": "other"}),
    ):
        with pytest.raises((TypeError, PermissionError, ValueError)):
            operation()

    object.__setattr__(execution, "owner_user_id", "other")
    with pytest.raises(PermissionError):
        execution.start_step(StepKind.ORDER, owner=owner)


def test_execution_preview_expiry_and_confirmed_binding_cannot_be_rebound():
    owner, intent, execution = setup()
    with pytest.raises(PermissionError):
        execution._clone(preview_expires_at=datetime.now(UTC) + timedelta(minutes=5))
    with pytest.raises(PermissionError):
        execution._clone(preview_expires_at=None)
    with pytest.raises(PermissionError):
        execution._clone(confirmation_id="forged", confirmation_digest="forged")
    confirmed = execution
    with pytest.raises(PermissionError):
        confirmed._clone(confirmation_id="other")
    with pytest.raises(PermissionError):
        confirmed._clone(confirmation_digest="other")


def test_finite_signed_zero_extreme_exponents_are_canonical_but_nonzero_extremes_fail():
    common = dict(
        owner_user_id="user-1", task_id="task-1", execution_id="exec-1",
        action=FulfillmentAction.ORDER_AND_PAY, confirmation_id="c",
        confirmation_digest="cd", step="order", preview_digest="p",
        currency="CNY", ordered_items_digest="items", canonical_args={},
    )
    keys = {
        command_idempotency_key(**common, amount=Decimal(value))
        for value in ("0E+19", "-0E-19", "0E+100", "-0.00")
    }
    assert len(keys) == 1
    for value in ("1E+19", "1E-19", "-1E+100"):
        with pytest.raises(ValueError):
            command_idempotency_key(**common, amount=Decimal(value))


def test_decimal_signed_zero_has_one_canonical_snapshot_and_command_key():
    values = [0, Decimal("0"), Decimal("-0"), Decimal("0.00"), Decimal("-0.00")]
    common = dict(
        owner_user_id="user-1", task_id="task-1", execution_id="exec-1",
        action=FulfillmentAction.ORDER_AND_PAY, confirmation_id="c",
        confirmation_digest="cd", step="order", preview_digest="p",
        currency="CNY", ordered_items_digest="items", canonical_args={},
    )
    keys = {command_idempotency_key(**common, amount=value) for value in values}
    assert len(keys) == 1
    owner = OwnerPrincipal.issue("user-1")
    intents = [
        FulfillmentIntent(
            userId="user-1", taskId="task-1", executionId=f"exec-zero-{i}",
            previewDigest="p", action=FulfillmentAction.ORDER_AND_PAY,
            amount=value, currency="CNY", orderedItemsDigest="items",
        )
        for i, value in enumerate(values)
    ]
    assert {intent.amount for intent in intents} == {Decimal("0")}
    executions = [SagaExecution.from_intent(intent, owner=owner) for intent in intents]
    assert {execution.amount for execution in executions} == {Decimal("0")}


def test_amount_decimal_and_canonical_args_reject_nonfinite_exponent_or_non_plain_values():
    common = dict(owner_user_id="user-1", task_id="task-1", execution_id="exec-1", action=FulfillmentAction.ORDER_AND_PAY, confirmation_id="c", confirmation_digest="cd", step="order", preview_digest="p", currency="CNY", ordered_items_digest="items")
    for amount in (float("nan"), float("inf"), -float("inf"), Decimal("1e-19"), Decimal("1e100"), "12"):
        with pytest.raises(ValueError): command_idempotency_key(**common, amount=amount)
    for args in (("tuple",), {1: "int-key"}, {"q": float("nan")}, {"é": "unicode-key"}, {"q": (1, 2)}):
        with pytest.raises((ValueError, TypeError)):
            command_idempotency_key(**common, amount=Decimal("1"), canonical_args=args)


def test_all_runtime_enum_inputs_are_exact_and_backend_references_are_opaque_ids():
    owner, _, execution = setup()
    with pytest.raises(ValueError): execution.start_step("order", owner=owner)
    with pytest.raises(ValidationError): SagaStep(step=StepKind.ORDER, action="order_only", userId="u", executionId="e", previewDigest="p")
    with pytest.raises(ValidationError): SagaReceipt.model_validate({"executionId": "e", "userId": "u", "step": StepKind.ORDER, "status": "CREATING", "previewDigest": "p", "commandIdempotencyKey": "x"})
    for reference in ("order-1", "abc:def", "customer-requested-no-payment", "order-xyz!", "order 1"):
        if reference == "order-1":
            started = execution.start_step(StepKind.ORDER, owner=owner)
            assert started.record_step_success(StepKind.ORDER, backend_reference=reference, owner=owner).status is FulfillmentStatus.ORDER_CREATED
        else:
            with pytest.raises((ValueError, ValidationError)):
                setup()[2].start_step(StepKind.ORDER, owner=owner).record_step_success(StepKind.ORDER, backend_reference=reference, owner=owner)


def test_compensated_is_closed_against_successful_payment_or_fulfillment_and_receipt_args_are_detached():
    owner, _, execution = setup()
    running = execution.start_step(StepKind.ORDER, owner=owner)
    exposed = running.step(StepKind.ORDER)
    assert exposed is not None
    exposed.canonical_args["injected"] = {"nested": True}
    assert "injected" not in running.step(StepKind.ORDER).canonical_args
    receipt = running.receipt(StepKind.ORDER, owner=owner)
    receipt.canonical_args["injected"] = True
    assert "injected" not in receipt.model_dump().get("canonicalArgs", {})

    order = running.record_step_success(StepKind.ORDER, backend_reference="order-1", owner=owner)
    paid = order.start_step(StepKind.PAYMENT, owner=owner).record_step_success(StepKind.PAYMENT, backend_reference="payment-1", owner=owner)
    with pytest.raises((ValidationError, ValueError)):
        paid.model_validate({**paid.model_dump(), "status": FulfillmentStatus.COMPENSATED, "compensationStatus": CompensationStatus.COMPLETED, "compensationReason": "PAYMENT_FAILED"})

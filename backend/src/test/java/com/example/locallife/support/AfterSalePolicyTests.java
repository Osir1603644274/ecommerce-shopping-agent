package com.example.locallife.support;

import org.junit.jupiter.api.Test;
import java.time.Instant;
import static org.assertj.core.api.Assertions.assertThat;
import static com.example.locallife.support.AfterSalePolicy.*;

class AfterSalePolicyTests {
    private final Instant received = Instant.parse("2026-09-01T00:00:00Z");
    private Facts facts(String status, long fence, Instant time, int refunded, int held, boolean active) {
        return new Facts("PAID", true, status, fence, time, 3, refunded, held, active, "PRODUCT:41", "black/256GB");
    }
    private Request request(Type type, int quantity) { return new Request(type, quantity, "PRODUCT:41", "black/256GB"); }

    @Test void includesExactDeadlineButRejectsOneNanosecondLater() {
        var f = facts("RECEIVED", 1, received, 0, 0, false);
        Instant deadline = received.plus(APPLICATION_WINDOW);
        assertThat(evaluate(f, request(Type.RETURN_REFUND, 1), deadline).route()).isEqualTo(Route.RETURN);
        assertThat(evaluate(f, request(Type.RETURN_REFUND, 1), deadline.plusNanos(1)).code())
                .isEqualTo("APPLICATION_WINDOW_EXPIRED");
    }
    @Test void missingOrFutureReceiptCannotAuthorizeRefund() {
        for (Instant time : new Instant[]{null, received.plusSeconds(1)})
            assertThat(evaluate(facts("RECEIVED", 1, time, 0, 0, false), request(Type.REFUND_ONLY, 1), received).route())
                    .isEqualTo(Route.VERIFY);
    }
    @Test void heldAndRefundedUnitsAreUnavailable() {
        var f = facts("RECEIVED", 1, received, 1, 1, false);
        assertThat(evaluate(f, request(Type.RETURN_REFUND, 2), received).code()).isEqualTo("QUANTITY_EXCEEDS_AVAILABLE");
        assertThat(evaluate(f, request(Type.RETURN_REFUND, 1), received).availableQuantity()).isEqualTo(1);
    }
    @Test void activeOperationBlocksEvenDifferentSku() {
        assertThat(evaluate(facts("RECEIVED", 1, received, 0, 0, true), request(Type.EXCHANGE, 1), received).code())
                .isEqualTo("ORDER_OPERATION_IN_PROGRESS");
    }
    @Test void onlyNeverClaimedDispatchUsesOldRefundPath() {
        assertThat(evaluate(facts("READY", 0, null, 0, 0, false), request(Type.REFUND_ONLY, 1), received).route())
                .isEqualTo(Route.UNSHIPPED_REFUND);
        assertThat(evaluate(facts("READY", 1, null, 0, 0, false), request(Type.REFUND_ONLY, 1), received).route())
                .isEqualTo(Route.VERIFY);
        assertThat(evaluate(facts("READY", 0, null, 0, 0, false), request(Type.EXCHANGE, 1), received).eligible()).isFalse();
    }
    @Test void shippedAndUnknownNeedVerificationNotAutomaticRefund() {
        for (String status : new String[]{"SHIPPED", "UNKNOWN", "DISPATCHING", "NEEDS_REVIEW"})
            assertThat(evaluate(facts(status, 1, null, 0, 0, false), request(Type.REFUND_ONLY, 1), received).eligible()).isFalse();
    }
    @Test void exchangeRequiresExactItemAndSpecification() {
        var f = facts("RECEIVED", 1, received, 0, 0, false);
        assertThat(evaluate(f, request(Type.EXCHANGE, 1), received).route()).isEqualTo(Route.RETURN);
        for (Request r : new Request[]{new Request(Type.EXCHANGE, 1, "PRODUCT:42", "black/256GB"),
                new Request(Type.EXCHANGE, 1, "PRODUCT:41", "black/128GB")})
            assertThat(evaluate(f, r, received).code()).isEqualTo("EXCHANGE_MUST_MATCH_PURCHASE");
    }
    @Test void deliveredRefundOnlyRequiresApproval() {
        assertThat(evaluate(facts("RECEIVED", 1, received, 0, 0, false), request(Type.REFUND_ONLY, 1), received).route())
                .isEqualTo(Route.REVIEW);
    }
    @Test void corruptCountersFailClosedEvenWithIntegerOverflow() {
        var f = new Facts("PAID", true, "RECEIVED", 1, received, Integer.MAX_VALUE,
                Integer.MAX_VALUE, 1, false, "41", "spec");
        assertThat(evaluate(f, request(Type.REFUND_ONLY, 1), received).code()).isEqualTo("INVALID_QUANTITY_EVIDENCE");
    }
    @Test void unpaidOrderAndNonpositiveQuantityCannotApply() {
        var f = new Facts("PENDING_PAYMENT", false, "READY", 0, null, 3, 0, 0, false, "41", "spec");
        assertThat(evaluate(f, request(Type.REFUND_ONLY, 1), received).code()).isEqualTo("PAYMENT_NOT_PAID");
        assertThat(evaluate(facts("RECEIVED", 1, received, 0, 0, false), request(Type.REFUND_ONLY, 0), received).code())
                .isEqualTo("INVALID_REQUEST");
    }
    @Test void existingCompleteOrderRouteRemainsEligibleButNeedsSuccessfulPayment() {
        var completed = new Facts("COMPLETED", true, "RECEIVED", 1, received, 3, 0, 0, false, "41", "spec");
        assertThat(evaluate(completed, request(Type.RETURN_REFUND, 1), received).eligible()).isTrue();
        var unpaid = new Facts("COMPLETED", false, "RECEIVED", 1, received, 3, 0, 0, false, "41", "spec");
        assertThat(evaluate(unpaid, request(Type.RETURN_REFUND, 1), received).code()).isEqualTo("PAYMENT_NOT_PAID");
    }
}

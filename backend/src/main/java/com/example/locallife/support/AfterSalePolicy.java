package com.example.locallife.support;

import java.time.Duration;
import java.time.Instant;
import java.util.Objects;

/** Server-side eligibility for the explicitly scoped simulator policy, not a legal policy. */
public final class AfterSalePolicy {
    public static final String VERSION = "support-simulator-v1";
    public static final Duration APPLICATION_WINDOW = Duration.ofHours(168);

    private AfterSalePolicy() { }

    public enum Type { REFUND_ONLY, RETURN_REFUND, EXCHANGE }
    public enum Route { UNSHIPPED_REFUND, REVIEW, RETURN, VERIFY, REJECT }
    public record Facts(String orderStatus, boolean paymentConfirmed, String fulfillmentStatus, long dispatchFence,
                        Instant receivedAt, int purchasedQuantity, int refundedQuantity,
                        int heldQuantity, boolean activeOrderOperation,
                        String purchasedItemIdentity, String purchasedSpecification) { }
    public record Request(Type type, int quantity, String replacementItemIdentity,
                          String replacementSpecification) { }
    public record Decision(Route route, String code, int availableQuantity, Instant deadline) {
        public boolean eligible() {
            return route == Route.UNSHIPPED_REFUND || route == Route.REVIEW || route == Route.RETURN;
        }
    }

    /** Caller must authenticate ownership before loading facts; commit repeats this under order lock. */
    public static Decision evaluate(Facts facts, Request request, Instant now) {
        Objects.requireNonNull(facts); Objects.requireNonNull(request); Objects.requireNonNull(now);
        if (request.type() == null || request.quantity() <= 0)
            return decision(Route.REJECT, "INVALID_REQUEST", 0, null);
        if (facts.purchasedQuantity() <= 0 || facts.refundedQuantity() < 0 || facts.heldQuantity() < 0
                || (long) facts.refundedQuantity() + facts.heldQuantity() > facts.purchasedQuantity())
            return decision(Route.VERIFY, "INVALID_QUANTITY_EVIDENCE", 0, null);
        int available = facts.purchasedQuantity() - facts.refundedQuantity() - facts.heldQuantity();
        if (!facts.paymentConfirmed() || !("PAID".equals(facts.orderStatus()) || "COMPLETED".equals(facts.orderStatus())))
            return decision(Route.REJECT, "PAYMENT_NOT_PAID", available, null);
        if (facts.activeOrderOperation())
            return decision(Route.REJECT, "ORDER_OPERATION_IN_PROGRESS", available, null);
        if (request.quantity() > available)
            return decision(Route.REJECT, "QUANTITY_EXCEEDS_AVAILABLE", available, null);
        if (facts.dispatchFence() < 0)
            return decision(Route.VERIFY, "INVALID_DISPATCH_EVIDENCE", available, null);
        if ("WAITING_PAYMENT".equals(facts.fulfillmentStatus()) || "READY".equals(facts.fulfillmentStatus())) {
            if (facts.dispatchFence() != 0)
                return decision(Route.VERIFY, "DISPATCH_ALREADY_CLAIMED", available, null);
            return decision(request.type() == Type.REFUND_ONLY ? Route.UNSHIPPED_REFUND : Route.REJECT,
                    request.type() == Type.REFUND_ONLY ? "USE_EXISTING_UNSHIPPED_REFUND" : "NOT_RECEIVED",
                    available, null);
        }
        if (!"RECEIVED".equals(facts.fulfillmentStatus()))
            return decision(Route.VERIFY, "RECEIPT_NOT_CONFIRMED", available, null);
        if (facts.receivedAt() == null || facts.receivedAt().isAfter(now))
            return decision(Route.VERIFY, "INVALID_RECEIPT_TIME", available, null);
        Instant deadline = facts.receivedAt().plus(APPLICATION_WINDOW);
        if (now.isAfter(deadline))
            return decision(Route.REJECT, "APPLICATION_WINDOW_EXPIRED", available, deadline);
        if (request.type() == Type.EXCHANGE) {
            if (blank(facts.purchasedItemIdentity()) || blank(facts.purchasedSpecification()))
                return decision(Route.VERIFY, "MISSING_PURCHASE_SPECIFICATION", available, deadline);
            if (!facts.purchasedItemIdentity().equals(request.replacementItemIdentity())
                    || !facts.purchasedSpecification().equals(request.replacementSpecification()))
                return decision(Route.REJECT, "EXCHANGE_MUST_MATCH_PURCHASE", available, deadline);
        }
        return decision(request.type() == Type.REFUND_ONLY ? Route.REVIEW : Route.RETURN,
                request.type() == Type.REFUND_ONLY ? "REQUIRES_REVIEW" : "REQUIRES_RETURN_INSPECTION",
                available, deadline);
    }

    private static boolean blank(String value) { return value == null || value.isBlank(); }
    private static Decision decision(Route route, String code, int available, Instant deadline) {
        return new Decision(route, code, available, deadline);
    }
}

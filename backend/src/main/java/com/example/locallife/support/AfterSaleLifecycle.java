package com.example.locallife.support;

import com.example.locallife.common.BusinessConflictException;
import java.util.Objects;
import static com.example.locallife.support.AfterSalePolicy.Type;

/** Pure transition contract. Persistence, receipts and effects belong to the transactional service. */
public final class AfterSaleLifecycle {
    private AfterSaleLifecycle() { }
    public enum Phase {
        AWAITING_REVIEW, AWAITING_RETURN, RETURN_IN_TRANSIT, AWAITING_INSPECTION,
        REFUND_PENDING, WAITING_STOCK, WAITING_CHOICE, REPLACEMENT_READY,
        REPLACEMENT_SHIPPED, REPLACEMENT_RELEASING, COMPLETED, REJECTED, CANCELLED, NEEDS_REVIEW
    }
    public enum Event {
        APPROVE, REJECT, SUBMIT_RETURN, RETURN_RECEIVED, INSPECTION_ACCEPTED,
        INSPECTION_DISPUTED, REFUND_RECEIPT_CONFIRMED, STOCK_RESERVED,
        STOCK_UNAVAILABLE, CHOOSE_WAIT, CONFIRM_CONVERT_TO_REFUND,
        REPLACEMENT_DISPATCH_CONFIRMED, REPLACEMENT_RECEIPT_CONFIRMED, RECEIPT_MISMATCH, CANCEL,
        RESUME_RETURN_CHECK, RESUME_INSPECTION, BEGIN_REPLACEMENT_RELEASE, REPLACEMENT_RELEASED
    }
    public record State(Type type, Phase phase) {
        public State { Objects.requireNonNull(type); Objects.requireNonNull(phase); }
        public boolean terminal() { return phase == Phase.COMPLETED || phase == Phase.REJECTED || phase == Phase.CANCELLED; }
    }

    public static State initial(Type type) {
        return new State(type, type == Type.REFUND_ONLY ? Phase.AWAITING_REVIEW : Phase.AWAITING_RETURN);
    }

    /** An event name is not evidence: callers must verify actor and durable receipt before apply. */
    public static State apply(State state, Event event) {
        Objects.requireNonNull(state); Objects.requireNonNull(event);
        Type type = state.type(); Phase phase = state.phase();
        if (event == Event.CANCEL && (phase == Phase.AWAITING_REVIEW || phase == Phase.AWAITING_RETURN))
            return new State(type, Phase.CANCELLED);
        Phase next = switch (phase) {
            case AWAITING_REVIEW -> switch (event) {
                case APPROVE -> type == Type.REFUND_ONLY ? Phase.REFUND_PENDING : null;
                case REJECT -> Phase.REJECTED;
                default -> null;
            };
            case AWAITING_RETURN -> event == Event.SUBMIT_RETURN && type != Type.REFUND_ONLY ? Phase.RETURN_IN_TRANSIT : null;
            case RETURN_IN_TRANSIT -> switch(event) {
                case RETURN_RECEIVED -> Phase.AWAITING_INSPECTION;
                case RECEIPT_MISMATCH -> Phase.NEEDS_REVIEW;
                default -> null;
            };
            case AWAITING_INSPECTION -> switch (event) {
                case INSPECTION_ACCEPTED -> type == Type.EXCHANGE ? Phase.WAITING_STOCK
                        : type == Type.RETURN_REFUND ? Phase.REFUND_PENDING : null;
                case INSPECTION_DISPUTED, RECEIPT_MISMATCH -> Phase.NEEDS_REVIEW;
                default -> null;
            };
            case REFUND_PENDING -> event == Event.REFUND_RECEIPT_CONFIRMED && type != Type.EXCHANGE ? Phase.COMPLETED : null;
            case WAITING_STOCK -> type != Type.EXCHANGE ? null : switch (event) {
                case STOCK_RESERVED -> Phase.REPLACEMENT_READY;
                case STOCK_UNAVAILABLE -> Phase.WAITING_CHOICE;
                case CONFIRM_CONVERT_TO_REFUND -> Phase.REFUND_PENDING;
                default -> null;
            };
            case WAITING_CHOICE -> type != Type.EXCHANGE ? null : switch (event) {
                case CHOOSE_WAIT -> Phase.WAITING_STOCK;
                case CONFIRM_CONVERT_TO_REFUND -> Phase.REFUND_PENDING;
                default -> null;
            };
            case REPLACEMENT_READY -> type != Type.EXCHANGE ? null : switch(event) {
                case REPLACEMENT_DISPATCH_CONFIRMED -> Phase.REPLACEMENT_SHIPPED;
                case BEGIN_REPLACEMENT_RELEASE -> Phase.REPLACEMENT_RELEASING;
                default -> null;
            };
            case REPLACEMENT_RELEASING -> event == Event.REPLACEMENT_RELEASED && type == Type.EXCHANGE ? Phase.WAITING_CHOICE : null;
            case REPLACEMENT_SHIPPED -> event == Event.REPLACEMENT_RECEIPT_CONFIRMED && type == Type.EXCHANGE
                    ? Phase.COMPLETED : null;
            case NEEDS_REVIEW -> type == Type.REFUND_ONLY ? null : switch(event) {
                case RESUME_RETURN_CHECK -> Phase.RETURN_IN_TRANSIT;
                case RESUME_INSPECTION -> Phase.AWAITING_INSPECTION;
                default -> null;
            };
            default -> null;
        };
        if (next == null) throw new BusinessConflictException("售后状态不允许此操作：" + phase + "/" + event);
        return new State(event == Event.CONFIRM_CONVERT_TO_REFUND ? Type.RETURN_REFUND : type, next);
    }
}

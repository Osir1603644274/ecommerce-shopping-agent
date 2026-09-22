package com.example.locallife.support;

import com.example.locallife.common.BusinessConflictException;
import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.*;
import static com.example.locallife.support.AfterSalePolicy.Type;
import static com.example.locallife.support.AfterSaleLifecycle.*;

class AfterSaleLifecycleTests {
    private State acceptedReturn(Type type) {
        State state = initial(type);
        state = apply(state, Event.SUBMIT_RETURN);
        state = apply(state, Event.RETURN_RECEIVED);
        return apply(state, Event.INSPECTION_ACCEPTED);
    }

    @Test void refundApprovalDoesNotMeanRefundCompleted() {
        State pending = apply(initial(Type.REFUND_ONLY), Event.APPROVE);
        assertThat(pending.phase()).isEqualTo(Phase.REFUND_PENDING);
        assertThat(pending.terminal()).isFalse();
        assertThat(apply(pending, Event.REFUND_RECEIPT_CONFIRMED).phase()).isEqualTo(Phase.COMPLETED);
    }
    @Test void returnRequiresReceiptAndInspectionBeforeMoney() {
        State transit = apply(initial(Type.RETURN_REFUND), Event.SUBMIT_RETURN);
        assertThatThrownBy(() -> apply(transit, Event.REFUND_RECEIPT_CONFIRMED)).isInstanceOf(BusinessConflictException.class);
        assertThatThrownBy(() -> apply(transit, Event.INSPECTION_ACCEPTED)).isInstanceOf(BusinessConflictException.class);
        State inspected = acceptedReturn(Type.RETURN_REFUND);
        assertThat(inspected.phase()).isEqualTo(Phase.REFUND_PENDING);
    }
    @Test void exchangeDoesNotReserveStockBeforeInspectionOrChargeAgain() {
        State initial = initial(Type.EXCHANGE);
        assertThatThrownBy(() -> apply(initial, Event.STOCK_RESERVED)).isInstanceOf(BusinessConflictException.class);
        State inspected = acceptedReturn(Type.EXCHANGE);
        State reserved = apply(inspected, Event.STOCK_RESERVED);
        assertThat(reserved.phase()).isEqualTo(Phase.REPLACEMENT_READY);
        assertThatThrownBy(() -> apply(reserved, Event.REFUND_RECEIPT_CONFIRMED)).isInstanceOf(BusinessConflictException.class);
        State shipped = apply(reserved, Event.REPLACEMENT_DISPATCH_CONFIRMED);
        assertThat(shipped.terminal()).isFalse();
        assertThat(apply(shipped, Event.REPLACEMENT_RECEIPT_CONFIRMED).terminal()).isTrue();
    }
    @Test void shortageWaitsForChoiceAndConversionNeedsExplicitEvent() {
        State shortage = apply(acceptedReturn(Type.EXCHANGE), Event.STOCK_UNAVAILABLE);
        assertThat(shortage.phase()).isEqualTo(Phase.WAITING_CHOICE);
        assertThat(shortage.type()).isEqualTo(Type.EXCHANGE);
        assertThatThrownBy(() -> apply(shortage, Event.STOCK_RESERVED)).isInstanceOf(BusinessConflictException.class);
        State refund = apply(shortage, Event.CONFIRM_CONVERT_TO_REFUND);
        assertThat(refund.type()).isEqualTo(Type.RETURN_REFUND);
        assertThat(refund.phase()).isEqualTo(Phase.REFUND_PENDING);
        assertThat(apply(shortage, Event.CHOOSE_WAIT).phase()).isEqualTo(Phase.WAITING_STOCK);
    }
    @Test void cannotCancelReturnAlreadyHandedOverOrUnknownRefund() {
        State transit = apply(initial(Type.RETURN_REFUND), Event.SUBMIT_RETURN);
        State refund = apply(initial(Type.REFUND_ONLY), Event.APPROVE);
        for (State state : new State[]{transit, refund})
            assertThatThrownBy(() -> apply(state, Event.CANCEL)).isInstanceOf(BusinessConflictException.class);
        assertThat(apply(initial(Type.RETURN_REFUND), Event.CANCEL).phase()).isEqualTo(Phase.CANCELLED);
    }
    @Test void noConversionAfterReplacementReservationOrDispatch() {
        State ready = apply(acceptedReturn(Type.EXCHANGE), Event.STOCK_RESERVED);
        assertThatThrownBy(() -> apply(ready, Event.CONFIRM_CONVERT_TO_REFUND)).isInstanceOf(BusinessConflictException.class);
    }
    @Test void disputedInspectionCannotAutomaticallyRefund() {
        State received = apply(apply(initial(Type.RETURN_REFUND), Event.SUBMIT_RETURN), Event.RETURN_RECEIVED);
        State disputed = apply(received, Event.INSPECTION_DISPUTED);
        assertThat(disputed.phase()).isEqualTo(Phase.NEEDS_REVIEW);
        assertThatThrownBy(() -> apply(disputed, Event.REFUND_RECEIPT_CONFIRMED)).isInstanceOf(BusinessConflictException.class);
    }
    @Test void terminalEventReplayMustBeHandledByReceiptDeduplicationNotReducer() {
        State done = apply(apply(initial(Type.REFUND_ONLY), Event.APPROVE), Event.REFUND_RECEIPT_CONFIRMED);
        for (Event event : Event.values())
            assertThatThrownBy(() -> apply(done, event)).isInstanceOf(BusinessConflictException.class);
    }
}

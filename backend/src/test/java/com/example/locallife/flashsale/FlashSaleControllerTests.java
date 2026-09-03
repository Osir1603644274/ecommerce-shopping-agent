package com.example.locallife.flashsale;

import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.Test;
import org.springframework.security.core.Authentication;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class FlashSaleControllerTests {
    @Test
    void purchaseClosesHttpObservationOnSuccessAndFailure() {
        SimpleMeterRegistry registry = new SimpleMeterRegistry();
        FlashSaleConcurrencyMetrics metrics = new FlashSaleConcurrencyMetrics(registry);
        FlashSaleService service = mock(FlashSaleService.class);
        Authentication authentication = mock(Authentication.class);
        when(authentication.getName()).thenReturn("user-1");
        FlashSalePurchaseResponse accepted = mock(FlashSalePurchaseResponse.class);
        when(service.purchase(1L, "user-1")).thenReturn(accepted)
                .thenThrow(new IllegalStateException("failure"));
        FlashSaleController controller = new FlashSaleController(service, metrics);

        assertThat(controller.purchase(1L, authentication).getStatusCode().value())
                .isEqualTo(202);
        assertThatThrownBy(() -> controller.purchase(1L, authentication))
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("failure");

        assertThat(registry.get(FlashSaleConcurrencyMetrics.HTTP_ACTIVE).gauge().value())
                .isZero();
        assertThat(registry.get(FlashSaleConcurrencyMetrics.HTTP_PEAK).gauge().value())
                .isEqualTo(1.0);
        assertThat(registry.counter(FlashSaleConcurrencyMetrics.HTTP_STARTED).count())
                .isEqualTo(2.0);
        assertThat(registry.counter(FlashSaleConcurrencyMetrics.HTTP_COMPLETED).count())
                .isEqualTo(2.0);
    }
}

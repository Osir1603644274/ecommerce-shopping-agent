package com.example.locallife.product;

import com.example.locallife.common.ApiResponse;
import com.example.locallife.common.ResourceNotFoundException;
import org.junit.jupiter.api.Test;
import java.util.Optional;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

class ProductPurchaseViewControllerTests {
    @Test void combinesCachedDetailsWithCurrentPurchaseConditions() {
        var products = mock(ProductService.class);
        var offers = mock(ProductOfferController.class);
        var detail = mock(ProductDetailResponse.class);
        var current = new ProductOfferController.View(200L, "CNY", "verified", 0, false, "ACTIVE");
        when(products.get(123L)).thenReturn(Optional.of(detail));
        when(offers.get(123L)).thenReturn(ApiResponse.ok(current));
        var result = new ProductPurchaseViewController(products, offers).get(123).data();
        assertThat(result.product()).isSameAs(detail);
        assertThat(result.offer()).isSameAs(current);
        assertThat(result.offer().canPurchase()).isFalse();
        verify(products).get(123L);
        verify(offers).get(123L);
    }
    @Test void missingProductDoesNotProduceAnOffer() {
        var products = mock(ProductService.class);
        var offers = mock(ProductOfferController.class);
        when(products.get(123L)).thenReturn(Optional.empty());
        assertThatThrownBy(() -> new ProductPurchaseViewController(products, offers).get(123))
            .isInstanceOf(ResourceNotFoundException.class);
        verifyNoInteractions(offers);
    }
}

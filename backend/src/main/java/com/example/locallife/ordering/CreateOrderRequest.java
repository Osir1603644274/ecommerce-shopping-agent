package com.example.locallife.ordering;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Pattern;

public record CreateOrderRequest(
        @NotNull
        @Pattern(regexp = "PRODUCT|LOCAL_DEAL", message = "itemType 仅支持 PRODUCT 或 LOCAL_DEAL")
        String itemType,
        @NotNull @Positive Long itemId,
        @NotNull @Positive Integer quantity,
        String userCouponId
) {
}

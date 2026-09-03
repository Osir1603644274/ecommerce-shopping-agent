package com.example.locallife.inventory;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Positive;

public record CreateStockRequest(
        @NotNull
        @Pattern(regexp = "PRODUCT|LOCAL_DEAL", message = "itemType 仅支持 PRODUCT 或 LOCAL_DEAL")
        String itemType,
        @NotNull @Positive Long itemId,
        @NotNull @Positive Integer quantity
) {
}

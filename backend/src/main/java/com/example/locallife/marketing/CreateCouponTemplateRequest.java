package com.example.locallife.marketing;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;

import java.time.LocalDateTime;

public record CreateCouponTemplateRequest(
        @NotBlank String name,
        @NotNull @Min(0) Long thresholdMinor,
        @NotNull @Positive Long discountMinor,
        @NotNull @Positive Integer totalQuantity,
        @NotNull LocalDateTime validFrom,
        @NotNull LocalDateTime validUntil
) {
}

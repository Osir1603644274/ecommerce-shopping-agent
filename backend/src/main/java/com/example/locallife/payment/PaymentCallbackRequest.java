package com.example.locallife.payment;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.PositiveOrZero;

public record PaymentCallbackRequest(
        @NotBlank String eventId,
        @NotBlank String paymentNo,
        @NotBlank String providerTradeNo,
        @NotNull @PositiveOrZero Long amountMinor,
        @NotBlank String status,
        @NotNull Long timestamp
) {
}

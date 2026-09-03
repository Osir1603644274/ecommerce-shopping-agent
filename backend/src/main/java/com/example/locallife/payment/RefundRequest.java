package com.example.locallife.payment;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record RefundRequest(
        @NotBlank @Size(max = 255) String reason
) {
}

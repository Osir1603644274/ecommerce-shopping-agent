package com.example.locallife.payment;

import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import java.util.List;

public record PartialRefundRequest(@NotEmpty @Size(max=50) List<@Valid Line> items,
                                   @NotBlank @Size(max=255) String reason, @PositiveOrZero Long expectedAmountMinor) {
    public PartialRefundRequest(List<Line> items,String reason) { this(items,reason,null); }
    public record Line(@NotNull @Positive Long itemId,@NotNull @Positive Integer quantity) { }
}

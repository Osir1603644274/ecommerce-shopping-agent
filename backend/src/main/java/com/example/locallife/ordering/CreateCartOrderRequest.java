package com.example.locallife.ordering;

import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import java.util.List;

public record CreateCartOrderRequest(@NotEmpty @Size(max=50) List<@Valid Line> items, String userCouponId,
                                    @PositiveOrZero Long expectedPayableMinor) {
    public CreateCartOrderRequest(List<Line> items, String userCouponId) { this(items,userCouponId,null); }
    public record Line(@NotNull @Pattern(regexp="PRODUCT") String itemType,
                       @NotNull @Positive Long itemId, @NotNull @Positive @Max(100000) Integer quantity,
                       @PositiveOrZero Long expectedUnitPriceMinor) {
        public Line(String itemType, Long itemId, Integer quantity) { this(itemType,itemId,quantity,null); }
    }
}

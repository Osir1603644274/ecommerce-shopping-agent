package com.example.locallife.product;

import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Size;

import java.util.List;

public record ResolveProductsRequest(
        @NotEmpty @Size(max = 10) List<Long> productIds
) {
}

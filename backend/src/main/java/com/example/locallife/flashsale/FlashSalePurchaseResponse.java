package com.example.locallife.flashsale;

public record FlashSalePurchaseResponse(
        String orderId,
        String status,
        String message
) {
}

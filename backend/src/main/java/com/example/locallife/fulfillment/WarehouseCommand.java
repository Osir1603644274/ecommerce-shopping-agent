package com.example.locallife.fulfillment;

public record WarehouseCommand(String requestKey, String orderId, String itemType, Long itemId, int quantity) { }

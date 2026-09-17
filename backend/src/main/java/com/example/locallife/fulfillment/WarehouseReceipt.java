package com.example.locallife.fulfillment;

public record WarehouseReceipt(String requestKey, String orderId, String commandHash, String trackingNo) { }

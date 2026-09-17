package com.example.locallife.fulfillment;

import java.time.LocalDateTime;

public record FulfillmentTask(String orderId, String requestKey, String commandJson, String status,
        int attempts, long fence, String owner, LocalDateTime leaseUntil, LocalDateTime nextAttemptAt,
        String trackingNo, String receiptJson, String lastError, LocalDateTime createdAt, LocalDateTime updatedAt) { }

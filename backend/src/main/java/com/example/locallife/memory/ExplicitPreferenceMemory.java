package com.example.locallife.memory;

import java.time.LocalDateTime;

/** Authoritative v2 row. Owner is never serialized in the user projection. */
record ExplicitPreferenceMemory(
        String id,
        String ownerUserId,
        String memoryCategory,
        String productCategory,
        String recipientScope,
        String source,
        String dataClass,
        String semanticKey,
        String tokenValue,
        int version,
        String status,
        LocalDateTime createdAt,
        LocalDateTime updatedAt,
        LocalDateTime expiresAt,
        String supersedesId
) {}

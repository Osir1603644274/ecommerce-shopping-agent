package com.example.locallife.memory;

import java.time.LocalDateTime;

record CatalogMemoryWrite(
        String id,
        String ownerUserId,
        String categoryId,
        String recipientScope,
        String source,
        String preferenceKind,
        String catalogRevision,
        String attributeKey,
        String normalizedValue,
        int version,
        String status,
        LocalDateTime expiresAt,
        String supersedesId,
        String commandDigest,
        String contentDigest
) {}

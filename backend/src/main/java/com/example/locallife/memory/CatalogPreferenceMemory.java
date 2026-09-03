package com.example.locallife.memory;

import java.time.LocalDateTime;

record CatalogPreferenceMemory(
        String id,
        String ownerUserId,
        String categoryId,
        String recipientScope,
        String source,
        int schemaVersion,
        String preferenceKind,
        String catalogRevision,
        String attributeKey,
        String normalizedValue,
        int version,
        String status,
        LocalDateTime createdAt,
        LocalDateTime updatedAt,
        LocalDateTime expiresAt,
        String supersedesId
) {}

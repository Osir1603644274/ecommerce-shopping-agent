package com.example.locallife.memory;

import java.time.Instant;
import java.util.List;

/** V13 catalog-bound projection. ownerBinding is non-reversible and request-local. */
public record MemoryProjectionV3Response(
        int schemaVersion,
        long revision,
        String ownerBinding,
        boolean truncated,
        List<Entry> entries
) {
    public MemoryProjectionV3Response {
        if (schemaVersion != 3 || revision < 0 || ownerBinding == null
                || !ownerBinding.matches("[0-9a-f]{64}")
                || entries == null || entries.size() > 8) {
            throw new IllegalArgumentException("invalid memory projection v3");
        }
        entries = List.copyOf(entries);
    }

    public record Entry(
            String entryId,
            String categoryId,
            String recipientScope,
            String preferenceKind,
            String attributeKey,
            String normalizedValue,
            String catalogRevision,
            int version,
            String status,
            Instant createdAt,
            Instant updatedAt,
            Instant expiresAt,
            String supersedes,
            boolean chainVerified
    ) {}
}

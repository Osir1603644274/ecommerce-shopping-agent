package com.example.locallife.memory;

import java.time.Instant;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.List;

/** Strict governed projection. MySQL revision and chain verification are authoritative. */
public record MemoryProjectionV2Response(
        int schemaVersion,
        long revision,
        String ownerBinding,
        boolean truncated,
        List<Entry> entries
) {
    public MemoryProjectionV2Response {
        if (schemaVersion != 2 || revision < 0 || ownerBinding == null
                || !ownerBinding.matches("[0-9a-f]{64}") || entries == null || entries.size() > 8) {
            throw new IllegalArgumentException("invalid memory projection v2");
        }
        entries = List.copyOf(entries);
    }

    static String bindOwner(String owner) {
        if (owner == null || owner.isBlank()) throw new IllegalArgumentException("invalid memory owner");
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(owner.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception impossible) {
            throw new IllegalStateException("owner binding unavailable", impossible);
        }
    }

    public record Entry(
            String entryId,
            String memoryCategory,
            String productCategory,
            String recipientScope,
            String semanticKey,
            String value,
            String source,
            String dataClass,
            int version,
            String status,
            Instant createdAt,
            Instant updatedAt,
            Instant expiresAt,
            String supersedes,
            boolean chainVerified
    ) {}
}

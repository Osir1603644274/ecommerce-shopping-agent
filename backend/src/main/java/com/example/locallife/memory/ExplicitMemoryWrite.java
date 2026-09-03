package com.example.locallife.memory;

import java.time.LocalDateTime;

/** Complete row required for a V12 write; never populated from an LLM payload. */
record ExplicitMemoryWrite(
        String id,
        String ownerUserId,
        String productCategory,
        String recipientScope,
        String source,
        String semanticKey,
        String tokenValue,
        int version,
        String status,
        LocalDateTime expiresAt,
        String supersedesId,
        String commandDigest,
        String contentDigest
) {}

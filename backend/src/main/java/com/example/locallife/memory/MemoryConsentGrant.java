package com.example.locallife.memory;

import java.time.Instant;

/** Safe-to-return receipt for a single user-confirmed memory command. */
public record MemoryConsentGrant(
        String commandId,
        String consentEventId,
        String consentAction,
        Instant expiresAt
) {}

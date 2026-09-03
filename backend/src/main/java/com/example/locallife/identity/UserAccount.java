package com.example.locallife.identity;

import java.time.LocalDateTime;

record UserAccount(
        String id,
        String username,
        String passwordHash,
        boolean enabled,
        int tokenVersion,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
}

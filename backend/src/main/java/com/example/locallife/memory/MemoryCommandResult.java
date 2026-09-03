package com.example.locallife.memory;

/** Persisted idempotency result.  PROCESSING is never returned as success. */
record MemoryCommandResult(
        String ownerUserId,
        String commandId,
        String requestDigest,
        String operation,
        String status,
        String memoryId,
        Integer memoryVersion,
        String memoryStatus
) {}

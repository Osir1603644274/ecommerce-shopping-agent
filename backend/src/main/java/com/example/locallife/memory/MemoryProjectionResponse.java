package com.example.locallife.memory;

import java.util.List;

/** Exact authenticated read envelope; revision is always MySQL authority. */
public record MemoryProjectionResponse(int schemaVersion, long revision, List<ShoppingMemoryService.MemoryResponse> entries) {
    public MemoryProjectionResponse { if (schemaVersion != 1 || revision < 0 || entries == null || entries.size() > 8) throw new IllegalArgumentException("invalid memory projection"); entries = List.copyOf(entries); }
}

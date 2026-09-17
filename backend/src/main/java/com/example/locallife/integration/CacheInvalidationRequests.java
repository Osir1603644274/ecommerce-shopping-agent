package com.example.locallife.integration;

/** Mutation-side port; implementations must persist in the caller's transaction. */
public interface CacheInvalidationRequests {
    void productUpdated(Long id);
    void shopUpdated(Long id);
}

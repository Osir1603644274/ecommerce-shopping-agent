package com.example.locallife.memory;

/** Failsafe-discoverable wrapper for the V11-to-V12 MySQL proof. */
class ShoppingMemoryV12MigrationContainerIT {
    @org.junit.jupiter.api.Test
    void upgradesExistingV11Rows() throws Exception {
        new ShoppingMemoryV12MigrationIT()
                .v12AllowsIndependentProductScopedChainsAndCreatesWriteAuthorityTables();
    }
}

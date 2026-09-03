package com.example.locallife.memory;

import org.junit.jupiter.api.Test;

/** Failsafe-discoverable wrapper for the target-10 to V11 MySQL proof. */
class ShoppingMemoryV11MigrationContainerIT {
    @Test void upgradesExistingV10Rows() throws Exception {
        new ShoppingMemoryV11MigrationIT().legacySensitiveRowsAreAuditedAndV2ScopeIsNeverInvented();
    }
}

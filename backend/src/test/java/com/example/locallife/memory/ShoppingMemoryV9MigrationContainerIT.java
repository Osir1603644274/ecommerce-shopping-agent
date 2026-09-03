package com.example.locallife.memory;

import org.junit.jupiter.api.Test;

/** Failsafe-discoverable wrapper for the target-8 to V9 MySQL upgrade proof. */
class ShoppingMemoryV9MigrationContainerIT {
    @Test void upgradesExistingV8Rows() throws Exception {
        new ShoppingMemoryV9MigrationIT().upgradesV8RowsIntoAuthoritativeProjectionHeads();
    }
}

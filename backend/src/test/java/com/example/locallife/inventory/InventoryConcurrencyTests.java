package com.example.locallife.inventory;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.review.ReviewVectorSyncClient;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest
class InventoryConcurrencyTests {
    private static final long ITEM_ID = 909090L;

    @Autowired
    private InventoryService service;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @AfterEach
    void cleanUp() {
        jdbcTemplate.update("""
                DELETE FROM inventory_reservation
                WHERE stock_id IN (
                    SELECT id FROM inventory_stock
                    WHERE item_type = 'PRODUCT' AND item_id = ?
                )
                """, ITEM_ID);
        jdbcTemplate.update(
                "DELETE FROM inventory_stock WHERE item_type = 'PRODUCT' AND item_id = ?",
                ITEM_ID);
    }

    @Test
    void conditionalUpdatePreventsOversellingUnderConcurrency() throws Exception {
        service.createStock("PRODUCT", ITEM_ID, 5);
        int contenders = 20;
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService executor = Executors.newFixedThreadPool(contenders);
        List<Callable<Boolean>> tasks = new ArrayList<>();
        for (int index = 0; index < contenders; index++) {
            tasks.add(() -> {
                start.await();
                try {
                    service.reserve(
                            UUID.randomUUID().toString(),
                            "PRODUCT",
                            ITEM_ID,
                            1,
                            LocalDateTime.now().plusMinutes(15)
                    );
                    return true;
                } catch (BusinessConflictException exception) {
                    return false;
                }
            });
        }

        List<Future<Boolean>> futures;
        try {
            futures = tasks.stream().map(executor::submit).toList();
            start.countDown();
            int successes = 0;
            for (Future<Boolean> future : futures) {
                if (future.get()) {
                    successes++;
                }
            }
            assertThat(successes).isEqualTo(5);
        } finally {
            executor.shutdownNow();
        }

        InventoryStock stock = service.getStock("PRODUCT", ITEM_ID);
        assertThat(stock.availableQuantity()).isZero();
        assertThat(stock.reservedQuantity()).isEqualTo(5);
    }
}

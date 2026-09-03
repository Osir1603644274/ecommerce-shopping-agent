package com.example.locallife.infrastructure;

import com.example.locallife.flashsale.FlashSaleCampaign;
import com.example.locallife.flashsale.FlashSaleConcurrencyMetrics;
import com.example.locallife.flashsale.FlashSaleProperties;
import com.example.locallife.flashsale.FlashSaleRedisGateway;
import com.example.locallife.identity.SlidingWindowRateLimiter;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.inventory.InventoryStock;
import com.example.locallife.ordering.CreateOrderRequest;
import com.example.locallife.ordering.OrderService;
import com.example.locallife.product.ProductCache;
import com.example.locallife.product.ProductDetailResponse;
import com.example.locallife.review.ReviewVectorSyncClient;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.data.redis.connection.RedisConnection;
import org.springframework.data.redis.listener.ChannelTopic;
import org.springframework.data.redis.listener.RedisMessageListenerContainer;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.MySQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

@Testcontainers
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
@SpringBootTest(properties = {
        "spring.sql.init.mode=never",
        "spring.flyway.enabled=true",
        "spring.flyway.baseline-on-migrate=false",
        "local-life.search.enabled=false",
        "local-life.messaging.enabled=false",
        "local-life.flash-sale.enabled=false",
        "local-life.rate-limit.enabled=false",
        "local-life.order.expiry-enabled=false",
        "local-life.memory.enabled=false",
        "local-life.cache.shop.enabled=false",
        "local-life.cache.product.enabled=false",
        "local-life.auth.secret=backend-reliability-test-secret-that-is-at-least-thirty-two-bytes",
        "local-life.auth.refresh-store=redis",
        "agent.review-sync.mode=direct"
})
class BackendReliabilityContainerIT {
    @Container
    static final MySQLContainer<?> MYSQL = new MySQLContainer<>("mysql:8.4")
            .withDatabaseName("backend_reliability")
            .withUsername("local_life")
            .withPassword("local_life_password")
            .withStartupTimeout(Duration.ofMinutes(2));

    @Container
    static final GenericContainer<?> REDIS = new GenericContainer<>("redis:7-alpine")
            .withExposedPorts(6379)
            .withStartupTimeout(Duration.ofMinutes(1));

    @DynamicPropertySource
    static void infrastructureProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", MYSQL::getJdbcUrl);
        registry.add("spring.datasource.username", MYSQL::getUsername);
        registry.add("spring.datasource.password", MYSQL::getPassword);
        registry.add("spring.datasource.driver-class-name", MYSQL::getDriverClassName);
        registry.add("spring.data.redis.host", REDIS::getHost);
        registry.add("spring.data.redis.port", () -> REDIS.getMappedPort(6379));
    }

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Autowired
    private StringRedisTemplate redisTemplate;

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    private InventoryService inventoryService;

    @Autowired
    private OrderService orderService;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @AfterEach
    void clearRedis() {
        redisTemplate.execute((RedisConnection connection) -> {
            connection.serverCommands().flushDb();
            return null;
        });
    }

    @Test
    void concurrentExpiryHasOneWinnerAndOneCompensationEventOnMysql() throws Exception {
        String userId = UUID.randomUUID().toString();
        long productId = 9_300_001L;
        jdbcTemplate.update("""
                INSERT INTO user_account(id, username, password_hash, enabled, token_version)
                VALUES (?, ?, 'unused', TRUE, 0)
                """, userId, "expiry-" + UUID.randomUUID());
        jdbcTemplate.update("""
                INSERT INTO product(
                    id, source, source_item_id, title, brand, seller,
                    category_l1, category_l2, category_l3,
                    snapshot_price_minor, currency, price_status, attribute_text,
                    data_nature, dataset_revision, source_license, provenance_url
                ) VALUES (?, 'fixture', ?, 'Expiry Phone', 'Fixture', 'Fixture Seller',
                    'digital', 'phone', 'smartphone', 199900, 'CNY', 'verified',
                    '12GB+256GB', 'deterministic_fixture', 'reliability-v1',
                    'test-only', 'fixture://expiry-product')
                """, productId, "expiry-" + productId);
        inventoryService.createStock("PRODUCT", productId, 5);
        var created = orderService.create(
                new CreateOrderRequest("PRODUCT", productId, 2, null),
                userId,
                "expiry-race"
        );
        jdbcTemplate.update(
                "UPDATE customer_order SET expires_at = UTC_TIMESTAMP() - INTERVAL 1 MINUTE WHERE id = ?",
                created.id()
        );

        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<Integer> first = pool.submit(() -> {
                start.await();
                return orderService.expireBatch(20);
            });
            Future<Integer> second = pool.submit(() -> {
                start.await();
                return orderService.expireBatch(20);
            });
            start.countDown();
            assertThat(first.get(10, TimeUnit.SECONDS) + second.get(10, TimeUnit.SECONDS))
                    .isEqualTo(1);
        } finally {
            pool.shutdownNow();
        }

        InventoryStock stock = inventoryService.getStock("PRODUCT", productId);
        assertThat(stock.availableQuantity()).isEqualTo(5);
        assertThat(stock.reservedQuantity()).isZero();
        assertThat(jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM outbox_event WHERE aggregate_id = ? AND event_type = 'order.expired.v1'",
                Integer.class,
                created.id()
        )).isEqualTo(1);
    }

    @Test
    void redisSlidingWindowAllowsExactlyConfiguredConcurrentQuota() throws Exception {
        SlidingWindowRateLimiter limiter = new SlidingWindowRateLimiter(redisTemplate);
        int contenders = 100;
        int limit = 20;
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(20);
        try {
            List<Future<Boolean>> results = java.util.stream.IntStream.range(0, contenders)
                    .mapToObj(index -> pool.submit(() -> {
                        start.await();
                        return limiter.acquire("benchmark:user-1", limit, Duration.ofMinutes(1))
                                .allowed();
                    }))
                    .toList();
            start.countDown();
            long allowed = 0;
            for (Future<Boolean> result : results) {
                if (result.get(10, TimeUnit.SECONDS)) {
                    allowed++;
                }
            }
            assertThat(allowed).isEqualTo(limit);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void flashSaleLuaAcceptsExactlyStockWithoutOverselling() throws Exception {
        FlashSaleProperties properties = new FlashSaleProperties(
                true, "stream.flash-sale-reliability", "reliability-group",
                Duration.ofSeconds(1), 3
        );
        FlashSaleRedisGateway gateway = new FlashSaleRedisGateway(
                redisTemplate, properties, new FlashSaleConcurrencyMetrics(new SimpleMeterRegistry()));
        long campaignId = 9_400_001L;
        int stock = 50;
        gateway.rebuild(new FlashSaleCampaign(
                campaignId, "PRODUCT", 1001L, "Reliability Flash Sale", 99900L,
                stock, stock, LocalDateTime.now().minusMinutes(1),
                LocalDateTime.now().plusMinutes(10), "ACTIVE", 1L, null, null
        ), List.of());

        int contenders = 200;
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(40);
        try {
            List<Future<Long>> results = java.util.stream.IntStream.range(0, contenders)
                    .mapToObj(index -> pool.submit(() -> {
                        start.await();
                        return gateway.purchase(
                                campaignId,
                                UUID.randomUUID().toString(),
                                "flash-user-" + index,
                                99900L
                        );
                    }))
                    .toList();
            start.countDown();
            long accepted = 0;
            long soldOut = 0;
            for (Future<Long> result : results) {
                long outcome = result.get(10, TimeUnit.SECONDS);
                accepted += outcome == FlashSaleRedisGateway.ACCEPTED ? 1 : 0;
                soldOut += outcome == FlashSaleRedisGateway.SOLD_OUT ? 1 : 0;
            }
            assertThat(accepted).isEqualTo(stock);
            assertThat(soldOut).isEqualTo(contenders - stock);
            assertThat(redisTemplate.opsForStream().size(properties.stream())).isEqualTo(stock);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void redisPubSubInvalidatesAnotherInstancesCaffeineEntry() throws Exception {
        ProductCache publisher = productCache();
        ProductCache remote = productCache();
        String redisKey = "local-life:product:detail:v1:1001";
        ProductDetailResponse oldDetail = detail("旧标题", 1L);
        ProductDetailResponse freshDetail = detail("新标题", 2L);
        redisTemplate.opsForValue().set(redisKey, objectMapper.writeValueAsString(oldDetail));
        assertThat(remote.getDetail(1001L).detail().title()).isEqualTo("旧标题");

        CountDownLatch invalidated = new CountDownLatch(1);
        RedisMessageListenerContainer container = new RedisMessageListenerContainer();
        container.setConnectionFactory(redisTemplate.getConnectionFactory());
        container.addMessageListener((message, pattern) -> {
            String body = new String(message.getBody(), StandardCharsets.UTF_8);
            if (body.equals("product:1001")) {
                remote.invalidateLocal(1001L);
                invalidated.countDown();
            }
        }, new ChannelTopic(ProductCache.INVALIDATION_CHANNEL));
        container.afterPropertiesSet();
        container.start();
        try {
            publisher.deleteDetail(1001L);
            assertThat(invalidated.await(5, TimeUnit.SECONDS)).isTrue();
            redisTemplate.opsForValue().set(redisKey, objectMapper.writeValueAsString(freshDetail));
            assertThat(remote.getDetail(1001L).detail().title()).isEqualTo("新标题");
        } finally {
            container.stop();
        }
    }

    private ProductCache productCache() {
        return new ProductCache(redisTemplate, objectMapper, 60, 2, 10,
                new SimpleMeterRegistry());
    }

    private static ProductDetailResponse detail(String title, long version) {
        return new ProductDetailResponse(
                1001L, "fixture", "1001", title, "Fixture", "Fixture Seller",
                "digital", "phone", "smartphone", 199900L, "CNY", "verified",
                "ACTIVE", version, 5, 1L, "", "deterministic_fixture",
                "reliability-v1", "test-only", "fixture://product", null, List.of()
        );
    }
}

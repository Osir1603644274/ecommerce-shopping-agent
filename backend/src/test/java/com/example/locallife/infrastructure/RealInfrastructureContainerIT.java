package com.example.locallife.infrastructure;

import com.example.locallife.inventory.InventoryService;
import com.example.locallife.memory.MemoryWriteRequest;
import com.example.locallife.memory.ShoppingMemoryService;
import com.example.locallife.ordering.CreateOrderRequest;
import com.example.locallife.ordering.OrderResponse;
import com.example.locallife.ordering.OrderService;
import com.example.locallife.review.ReviewVectorSyncClient;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.MySQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.utility.DockerImageName;

import java.time.Duration;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Map;
import java.util.TreeMap;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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
        "local-life.auth.secret=testcontainers-jwt-secret-that-is-at-least-thirty-two-bytes",
        "local-life.auth.refresh-store=redis",
        "local-life.cache.shop.enabled=false",
        "local-life.cache.product.enabled=false",
        "agent.review-sync.mode=direct"
})
@Import(RealInfrastructureContainerIT.MapperCounterConfiguration.class)
class RealInfrastructureContainerIT {
    static final AtomicInteger MEMORY_PROJECTION_QUERIES = new AtomicInteger();
    @TestConfiguration
    static class MapperCounterConfiguration {
        @Bean org.apache.ibatis.plugin.Interceptor memoryProjectionCounter() { return new MemoryProjectionCounter(); }
    }
    @org.apache.ibatis.plugin.Intercepts({
            @org.apache.ibatis.plugin.Signature(type=org.apache.ibatis.executor.Executor.class,method="query",args={org.apache.ibatis.mapping.MappedStatement.class,Object.class,org.apache.ibatis.session.RowBounds.class,org.apache.ibatis.session.ResultHandler.class}),
            @org.apache.ibatis.plugin.Signature(type=org.apache.ibatis.executor.Executor.class,method="query",args={org.apache.ibatis.mapping.MappedStatement.class,Object.class,org.apache.ibatis.session.RowBounds.class,org.apache.ibatis.session.ResultHandler.class,org.apache.ibatis.cache.CacheKey.class,org.apache.ibatis.mapping.BoundSql.class})})
    static class MemoryProjectionCounter implements org.apache.ibatis.plugin.Interceptor {
        @Override public Object intercept(org.apache.ibatis.plugin.Invocation invocation) throws Throwable { if(invocation.getArgs()[0] instanceof org.apache.ibatis.mapping.MappedStatement statement && statement.getId().equals("com.example.locallife.memory.MemoryMapper.projection")) MEMORY_PROJECTION_QUERIES.incrementAndGet(); return invocation.proceed(); }
    }
    private static final DockerImageName MYSQL_IMAGE =
            DockerImageName.parse("mysql:8.4");
    private static final DockerImageName REDIS_IMAGE =
            DockerImageName.parse("redis:7-alpine");

    @Container
    static final MySQLContainer<?> MYSQL = new MySQLContainer<>(MYSQL_IMAGE)
            .withDatabaseName("local_life_test")
            .withUsername("local_life")
            .withPassword("public-demo-secret-2-change-before-use")
            .withCommand("--log-bin-trust-function-creators=1")
            .withStartupTimeout(Duration.ofMinutes(2));

    @Container
    static final GenericContainer<?> REDIS = new GenericContainer<>(REDIS_IMAGE)
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
    private InventoryService inventoryService;

    @Autowired
    private OrderService orderService;

    @Autowired
    private ShoppingMemoryService shoppingMemoryService;


    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @Test
    void migrationsRedisAndTransactionalOrderWorkOnProductionEngines() {
        Integer migrationCount = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM flyway_schema_history WHERE success = 1",
                Integer.class
        );
        assertThat(migrationCount).isEqualTo(9);

        String redisKey = "testcontainers:probe:" + UUID.randomUUID();
        redisTemplate.opsForValue().set(redisKey, "ready", Duration.ofSeconds(30));
        assertThat(redisTemplate.opsForValue().get(redisKey)).isEqualTo("ready");
        assertThat(redisTemplate.delete(redisKey)).isTrue();

        String userId = UUID.randomUUID().toString();
        long productId = 9_100_001L;
        jdbcTemplate.update("""
                INSERT INTO user_account(id, username, password_hash, enabled, token_version)
                VALUES (?, ?, 'unused', TRUE, 0)
                """, userId, "container-" + UUID.randomUUID());
        jdbcTemplate.update("""
                INSERT INTO product(
                    id, source, source_item_id, title, brand, seller,
                    category_l1, category_l2, category_l3,
                    snapshot_price_minor, currency, price_status, attribute_text,
                    data_nature, dataset_revision, source_license, provenance_url
                ) VALUES (?, 'fixture', ?, 'Container Phone', 'Fixture', 'Fixture Seller',
                    'digital', 'phone', 'smartphone',
                    199900, 'CNY', 'verified', '12GB+256GB',
                    'deterministic_fixture', 'container-v1', 'test-only', 'fixture://product')
                """, productId, "container-" + productId);
        inventoryService.createStock("PRODUCT", productId, 3);

        CreateOrderRequest request = new CreateOrderRequest("PRODUCT", productId, 2, null);
        var preview = orderService.preview(request, userId);
        assertThat(preview.unitPriceMinor()).isEqualTo(199900L);
        assertThat(preview.payableMinor()).isEqualTo(399800L);
        assertThat(preview.availableQuantity()).isEqualTo(3);

        OrderResponse created = orderService.create(request, userId, "container-checkout-1");
        OrderResponse replay = orderService.create(request, userId, "container-checkout-1");
        assertThat(replay.id()).isEqualTo(created.id());
        assertThat(inventoryService.getStock("PRODUCT", productId).availableQuantity())
                .isEqualTo(1);
        assertThat(inventoryService.getStock("PRODUCT", productId).reservedQuantity())
                .isEqualTo(2);
        assertThat(jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM outbox_event WHERE aggregate_id = ?",
                Integer.class,
                created.id()
        )).isEqualTo(1);

        MemoryWriteRequest memoryRequest = memoryWriteRequest("container-memory-command", "container-memory-consent");
        var memory = shoppingMemoryService.write(userId, memoryRequest);
        assertThat(memory.status()).isEqualTo("ACTIVE");
        assertThat(jdbcTemplate.queryForObject("SELECT revision FROM user_memory_projection_head WHERE owner_user_id=?", Long.class, userId)).isEqualTo(1L);
        assertThat(shoppingMemoryService.projection(userId)).extracting("value").contains("brand-x");
        MEMORY_PROJECTION_QUERIES.set(0);
        assertThat(shoppingMemoryService.projection(userId)).extracting("value").contains("brand-x");
        assertThat(MEMORY_PROJECTION_QUERIES.get()).isZero();
        var cacheKeys = redisTemplate.keys("memory:projection:v1:*");
        assertThat(cacheKeys).isNotEmpty();
        String cacheKey = cacheKeys.stream().filter(key -> redisTemplate.opsForValue().get(key).contains("\"revision\":1")).findFirst().orElseThrow();
        String staleEnvelope = redisTemplate.opsForValue().get(cacheKey);
        assertThat(staleEnvelope).contains("\"revision\":1");
        MemoryWriteRequest update = memoryUpdateRequest("container-memory-update", "container-memory-update-consent", memory.entryId(), 1, "brand-a");
        var updated = shoppingMemoryService.write(userId, update);
        assertThat(jdbcTemplate.queryForObject("SELECT revision FROM user_memory_projection_head WHERE owner_user_id=?", Long.class, userId)).isEqualTo(2L);
        redisTemplate.opsForValue().set(cacheKey, staleEnvelope);
        assertThat(shoppingMemoryService.projection(userId)).extracting("value").contains("brand-a").doesNotContain("brand-x");
        assertThat(redisTemplate.opsForValue().get(cacheKey)).contains("\"revision\":2").isNotEqualTo(staleEnvelope);
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM user_shopping_memory WHERE owner_user_id=?", Integer.class, userId)).isEqualTo(2);
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM user_memory_audit WHERE owner_user_id=?", Integer.class, userId)).isEqualTo(2);
        jdbcTemplate.execute("CREATE TRIGGER memory_outbox_test_failure BEFORE INSERT ON outbox_event FOR EACH ROW BEGIN IF NEW.event_type = 'memory.changed.v1' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'test outbox failure'; END IF; END");
        try {
            MemoryWriteRequest rollback = memoryWriteRequest("rollback-command", "rollback-event", "prefer_category", "phone");
            assertThatThrownBy(() -> shoppingMemoryService.write(userId, rollback)).isInstanceOf(RuntimeException.class);
            assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM user_shopping_memory WHERE owner_user_id=? AND command_digest=?", Integer.class, userId, rollback.consent().commandDigest())).isZero();
            assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM user_memory_consent_consumption WHERE owner_user_id=? AND consent_event_id=?", Integer.class, userId, "rollback-event")).isZero();
            assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM user_memory_audit WHERE owner_user_id=? AND consent_event_id=?", Integer.class, userId, "rollback-event")).isZero();
            assertThat(jdbcTemplate.queryForObject("SELECT revision FROM user_memory_projection_head WHERE owner_user_id=?", Long.class, userId)).isEqualTo(2L);
        } finally {
            jdbcTemplate.execute("DROP TRIGGER memory_outbox_test_failure");
        }
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM outbox_event WHERE event_type='memory.changed.v1' AND aggregate_id IN (?,?)", Integer.class, memory.entryId(), updated.entryId())).isEqualTo(2);
        assertThatThrownBy(() -> jdbcTemplate.update("INSERT INTO user_memory_consent_consumption(owner_user_id,consent_event_id,command_digest,content_digest) VALUES(?,?,?,?)", userId, "container-memory-consent", memoryRequest.consent().commandDigest(), memoryRequest.consent().contentDigest()))
                .isInstanceOf(DuplicateKeyException.class);
        MemoryWriteRequest changedDigest = memoryWriteRequest("container-memory-command-2", "container-memory-consent", "avoid_material", "latex");
        assertThatThrownBy(() -> shoppingMemoryService.write(userId, changedDigest)).isInstanceOf(RuntimeException.class);
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM user_shopping_memory WHERE owner_user_id=?", Integer.class, userId)).isEqualTo(2);
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM user_memory_consent_consumption WHERE owner_user_id=?", Integer.class, userId)).isEqualTo(2);
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM user_memory_audit WHERE owner_user_id=?", Integer.class, userId)).isEqualTo(2);
    }

    private static MemoryWriteRequest memoryWriteRequest(String commandId, String eventId) {
        return memoryWriteRequest(commandId, eventId, "avoid_brand", "brand-x");
    }
    private static MemoryWriteRequest memoryWriteRequest(String commandId, String eventId, String key, String value) {
        MemoryWriteRequest.MemoryPreference preference = new MemoryWriteRequest.MemoryPreference("shopping_preference", key, value);
        String content = sha(Map.of("category", preference.category(), "semanticKey", preference.semanticKey(), "value", preference.value()));
        TreeMap<String, Object> command = new TreeMap<>();
        command.put("commandId", commandId); command.put("operation", "write");
        command.put("predecessorId", null); command.put("previousVersion", null); command.put("contentDigest", content);
        return new MemoryWriteRequest(commandId, "write", null, null, preference,
                new MemoryWriteRequest.MemoryConsent("grant", eventId, sha(command), content));
    }
    private static MemoryWriteRequest memoryUpdateRequest(String commandId, String eventId, String predecessor, int previousVersion, String value) {
        MemoryWriteRequest.MemoryPreference preference = new MemoryWriteRequest.MemoryPreference("shopping_preference", "avoid_brand", value);
        String content = sha(Map.of("category", preference.category(), "semanticKey", preference.semanticKey(), "value", preference.value()));
        TreeMap<String,Object> command = new TreeMap<>(); command.put("commandId",commandId); command.put("operation","update"); command.put("predecessorId",predecessor); command.put("previousVersion",previousVersion); command.put("contentDigest",content);
        return new MemoryWriteRequest(commandId,"update",predecessor,previousVersion,preference,new MemoryWriteRequest.MemoryConsent("grant",eventId,sha(command),content));
    }

    private static String sha(Object value) {
        try {
            return ShoppingMemoryService.digest(value);
        } catch (Exception error) { throw new AssertionError(error); }
    }
}

package com.example.locallife.flashsale;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.MySQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;
import static org.assertj.core.api.Assertions.*;

@Testcontainers
@SpringBootTest(properties={"spring.sql.init.mode=never","spring.flyway.enabled=true",
        "spring.flyway.baseline-on-migrate=false","local-life.messaging.enabled=false",
        "local-life.flash-sale.enabled=false","local-life.order.expiry-enabled=false"})
class FlashSaleRecoveryContainerIT extends FlashSalePersistenceIntegrationTests {
    @Container static final MySQLContainer<?> MYSQL=new MySQLContainer<>("mysql:8.4")
            .withDatabaseName("mq_recovery").withUsername("mq_test").withPassword("isolated-test-only");
    @Container static final GenericContainer<?> REDIS=new GenericContainer<>("redis:7-alpine").withExposedPorts(6379);
    @DynamicPropertySource static void properties(DynamicPropertyRegistry properties) {
        properties.add("spring.datasource.url",MYSQL::getJdbcUrl);
        properties.add("spring.datasource.username",MYSQL::getUsername);
        properties.add("spring.datasource.password",MYSQL::getPassword);
        properties.add("spring.datasource.driver-class-name",MYSQL::getDriverClassName);
        properties.add("spring.data.redis.host",REDIS::getHost);
        properties.add("spring.data.redis.port",()->REDIS.getMappedPort(6379));
    }
    @Autowired StringRedisTemplate redis;
    @Autowired JdbcTemplate jdbc;

    @Test void validatesMigrationAndReservationOwnershipOnRealRedis() {
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM flyway_schema_history WHERE success=1",Integer.class)).isEqualTo(17);
        var gateway=new FlashSaleRedisGateway(redis,new FlashSaleProperties(true,"stream.mq-test","group",Duration.ofSeconds(1),3),
                new FlashSaleConcurrencyMetrics(new SimpleMeterRegistry()));
        var campaign=new FlashSaleCampaign(99999L,"PRODUCT",1L,"fixture",10L,1,1,
                LocalDateTime.now().minusMinutes(1),LocalDateTime.now().plusMinutes(1),"ACTIVE",0L,null,null);
        gateway.rebuild(campaign,List.of());
        redis.opsForValue().set("stream.mq-test","wrong type");
        assertThatThrownBy(()->gateway.purchase(99999L,"order-1","buyer-1",10L)).isInstanceOf(RuntimeException.class);
        assertThat(redis.opsForValue().get("flash:{flash-sale}:campaign:99999:stock")).isEqualTo("1");
        redis.delete("stream.mq-test");
        assertThat(gateway.purchase(99999L,"order-1","buyer-1",10L)).isZero();
        gateway.compensate(99999L,"buyer-1","different-order");
        assertThat(redis.opsForValue().get("flash:{flash-sale}:campaign:99999:stock")).isEqualTo("0");
        gateway.compensate(99999L,"buyer-1","order-1");
        gateway.compensate(99999L,"buyer-1","order-1");
        assertThat(redis.opsForValue().get("flash:{flash-sale}:campaign:99999:stock")).isEqualTo("1");
        var reserved=new FlashSaleCampaign(99999L,"PRODUCT",1L,"fixture",10L,1,0,
                campaign.startsAt(),campaign.endsAt(),"ACTIVE",0L,null,null);
        gateway.rebuild(reserved,List.of("buyer-1"),java.util.Map.of("buyer-1","order-restored"));
        gateway.compensate(99999L,"buyer-1","order-restored");
        assertThat(redis.opsForValue().get("flash:{flash-sale}:campaign:99999:stock")).isEqualTo("1");
    }
}

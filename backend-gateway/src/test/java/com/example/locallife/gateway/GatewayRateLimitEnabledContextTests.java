package com.example.locallife.gateway;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.data.redis.core.ReactiveStringRedisTemplate;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest(properties = {
        "gateway.auth.secret=0123456789abcdef0123456789abcdef",
        "gateway.rate-limit.enabled=true"
})
class GatewayRateLimitEnabledContextTests {
    @Autowired
    private GatewayRateLimitFilter filter;

    @Autowired
    private ReactiveStringRedisTemplate redis;

    @Test
    void rateLimitBeansStartWithRedisClientEnabled() {
        assertThat(filter).isNotNull();
        assertThat(redis).isNotNull();
    }
}

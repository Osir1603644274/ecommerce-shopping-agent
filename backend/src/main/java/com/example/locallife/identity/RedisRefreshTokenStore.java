package com.example.locallife.identity;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

@Component
@ConditionalOnProperty(
        name = "local-life.auth.refresh-store",
        havingValue = "redis",
        matchIfMissing = true
)
class RedisRefreshTokenStore implements RefreshTokenStore {

    private static final String TOKEN_PREFIX = "auth:refresh:";
    private static final String FAMILY_PREFIX = "auth:refresh-family:";
    private static final DefaultRedisScript<Long> CONSUME_SCRIPT = new DefaultRedisScript<>(
            """
            local value = redis.call('get', KEYS[1])
            if not value or value ~= ARGV[1] then
                return 0
            end
            redis.call('del', KEYS[1])
            redis.call('srem', KEYS[2], ARGV[2])
            return 1
            """,
            Long.class
    );

    private final StringRedisTemplate redis;

    RedisRefreshTokenStore(StringRedisTemplate redis) {
        this.redis = redis;
    }

    @Override
    public void save(String jti, String familyId, String tokenHash, Duration ttl) {
        String tokenKey = TOKEN_PREFIX + jti;
        String familyKey = FAMILY_PREFIX + familyId;
        redis.opsForValue().set(tokenKey, tokenHash, ttl);
        redis.opsForSet().add(familyKey, jti);
        redis.expire(familyKey, ttl.plus(Duration.ofMinutes(5)));
    }

    @Override
    public boolean consume(String jti, String familyId, String tokenHash) {
        Long result = redis.execute(
                CONSUME_SCRIPT,
                List.of(TOKEN_PREFIX + jti, FAMILY_PREFIX + familyId),
                tokenHash,
                jti
        );
        return Long.valueOf(1L).equals(result);
    }

    @Override
    public void revokeFamily(String familyId) {
        String familyKey = FAMILY_PREFIX + familyId;
        Set<String> members = redis.opsForSet().members(familyKey);
        List<String> keys = new ArrayList<>();
        if (members != null) {
            members.forEach(jti -> keys.add(TOKEN_PREFIX + jti));
        }
        keys.add(familyKey);
        redis.delete(keys);
    }
}

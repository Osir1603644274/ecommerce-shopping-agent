package com.example.locallife.identity;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.Instant;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

@Component
@ConditionalOnProperty(name = "local-life.auth.refresh-store", havingValue = "memory")
class InMemoryRefreshTokenStore implements RefreshTokenStore {

    private final ConcurrentHashMap<String, StoredToken> tokens = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, Set<String>> families = new ConcurrentHashMap<>();

    @Override
    public void save(String jti, String familyId, String tokenHash, Duration ttl) {
        tokens.put(jti, new StoredToken(familyId, tokenHash, Instant.now().plus(ttl)));
        families.computeIfAbsent(familyId, ignored -> ConcurrentHashMap.newKeySet()).add(jti);
    }

    @Override
    public boolean consume(String jti, String familyId, String tokenHash) {
        StoredToken stored = tokens.get(jti);
        if (stored == null
                || stored.expiresAt().isBefore(Instant.now())
                || !stored.familyId().equals(familyId)
                || !stored.tokenHash().equals(tokenHash)) {
            return false;
        }
        if (!tokens.remove(jti, stored)) {
            return false;
        }
        Set<String> members = families.get(familyId);
        if (members != null) {
            members.remove(jti);
        }
        return true;
    }

    @Override
    public void revokeFamily(String familyId) {
        Set<String> members = families.remove(familyId);
        if (members != null) {
            members.forEach(tokens::remove);
        }
    }

    private record StoredToken(String familyId, String tokenHash, Instant expiresAt) {
    }
}

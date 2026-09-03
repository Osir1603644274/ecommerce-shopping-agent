package com.example.locallife.identity;

import java.time.Duration;

interface RefreshTokenStore {

    void save(String jti, String familyId, String tokenHash, Duration ttl);

    boolean consume(String jti, String familyId, String tokenHash);

    void revokeFamily(String familyId);
}

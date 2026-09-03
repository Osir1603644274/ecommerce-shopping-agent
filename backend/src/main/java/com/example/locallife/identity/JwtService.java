package com.example.locallife.identity;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.Base64;
import java.util.List;
import java.util.UUID;

@Component
class JwtService {

    private final JwtEncoder encoder;
    private final JwtDecoder refreshDecoder;
    private final AuthProperties properties;

    JwtService(
            JwtEncoder encoder,
            @Qualifier("refreshJwtDecoder") JwtDecoder refreshDecoder,
            AuthProperties properties
    ) {
        this.encoder = encoder;
        this.refreshDecoder = refreshDecoder;
        this.properties = properties;
    }

    IssuedTokens issue(UserAccount account, List<String> roles, String familyId) {
        Instant now = Instant.now();
        String accessJti = UUID.randomUUID().toString();
        String refreshJti = UUID.randomUUID().toString();
        String effectiveFamilyId = familyId == null ? UUID.randomUUID().toString() : familyId;

        JwtClaimsSet accessClaims = JwtClaimsSet.builder()
                .issuer(properties.issuer())
                .issuedAt(now)
                .expiresAt(now.plus(properties.accessTtl()))
                .subject(account.id())
                .id(accessJti)
                .claim("type", "access")
                .claim("username", account.username())
                .claim("roles", roles)
                .claim("tokenVersion", account.tokenVersion())
                .build();
        JwtClaimsSet refreshClaims = JwtClaimsSet.builder()
                .issuer(properties.issuer())
                .issuedAt(now)
                .expiresAt(now.plus(properties.refreshTtl()))
                .subject(account.id())
                .id(refreshJti)
                .claim("type", "refresh")
                .claim("familyId", effectiveFamilyId)
                .claim("tokenVersion", account.tokenVersion())
                .build();

        return new IssuedTokens(
                encode(accessClaims),
                encode(refreshClaims),
                refreshJti,
                effectiveFamilyId
        );
    }

    RefreshClaims decodeRefresh(String token) {
        Jwt jwt = refreshDecoder.decode(token);
        if (!"refresh".equals(jwt.getClaimAsString("type"))) {
            throw new IllegalArgumentException("Token is not a refresh token");
        }
        String familyId = jwt.getClaimAsString("familyId");
        if (jwt.getId() == null || jwt.getSubject() == null || familyId == null) {
            throw new IllegalArgumentException("Refresh token is missing required claims");
        }
        return new RefreshClaims(jwt.getSubject(), jwt.getId(), familyId);
    }

    String hash(String token) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(token.getBytes(StandardCharsets.UTF_8));
            return Base64.getUrlEncoder().withoutPadding().encodeToString(digest);
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is not available", exception);
        }
    }

    long accessTtlSeconds() {
        return properties.accessTtl().toSeconds();
    }

    long refreshTtlSeconds() {
        return properties.refreshTtl().toSeconds();
    }

    java.time.Duration refreshTtl() {
        return properties.refreshTtl();
    }

    private String encode(JwtClaimsSet claims) {
        JwsHeader header = JwsHeader.with(MacAlgorithm.HS256).build();
        return encoder.encode(JwtEncoderParameters.from(header, claims)).getTokenValue();
    }

    record IssuedTokens(
            String accessToken,
            String refreshToken,
            String refreshJti,
            String familyId
    ) {
    }

    record RefreshClaims(String userId, String jti, String familyId) {
    }
}

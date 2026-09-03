package com.example.locallife.identity;

public record AuthTokensResponse(
        String accessToken,
        String refreshToken,
        String tokenType,
        long accessExpiresInSeconds,
        long refreshExpiresInSeconds,
        AuthUserResponse user
) {
}

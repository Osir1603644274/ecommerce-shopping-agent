package com.example.locallife.identity;

import java.util.List;

public record AuthenticatedIdentityResponse(
        String userId,
        List<String> roles
) {
}

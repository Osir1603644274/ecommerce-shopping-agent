package com.example.locallife.identity;

import java.util.List;

public record AuthUserResponse(String id, String username, List<String> roles) {
}

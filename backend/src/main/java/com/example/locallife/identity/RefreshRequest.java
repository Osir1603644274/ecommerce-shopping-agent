package com.example.locallife.identity;

import jakarta.validation.constraints.NotBlank;

public record RefreshRequest(
        @NotBlank(message = "刷新令牌不能为空") String refreshToken
) {
}

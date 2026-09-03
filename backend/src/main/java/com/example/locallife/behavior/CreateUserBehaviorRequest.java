package com.example.locallife.behavior;

import jakarta.validation.constraints.DecimalMax;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.time.LocalDateTime;

public record CreateUserBehaviorRequest(
        @NotBlank(message = "用户编号不能为空")
        @Size(max = 64, message = "用户编号不能超过 64 个字符")
        String userId,

        @NotNull(message = "商户编号不能为空")
        Long shopId,

        @NotBlank(message = "行为类型不能为空")
        @Size(max = 32, message = "行为类型不能超过 32 个字符")
        String behaviorType,

        @DecimalMin(value = "0.0", message = "行为分数不能小于 0")
        @DecimalMax(value = "5.0", message = "行为分数不能大于 5")
        Double score,

        @NotBlank(message = "行为来源不能为空")
        @Size(max = 32, message = "行为来源不能超过 32 个字符")
        String source,

        LocalDateTime occurredAt
) {
}
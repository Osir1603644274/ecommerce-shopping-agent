package com.example.locallife.shop;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public record UpdateShopRequest(
        @NotBlank(message = "商户地址不能为空")
        @Size(max = 255, message = "商户地址不能超过 255 个字符")
        String address,

        @NotNull(message = "人均价格不能为空")
        @Min(value = 0, message = "人均价格不能小于 0")
        @Max(value = 99999, message = "人均价格不能超过 99999")
        Integer avgPrice,

        @Size(max = 32, message = "商户电话不能超过 32 个字符")
        String phone
) {
}

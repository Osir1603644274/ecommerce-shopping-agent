package com.example.locallife.product;

import jakarta.validation.constraints.AssertTrue;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;

public record ProductUpdateRequest(
        @NotNull(message = "expectedVersion 不能为空")
        @Min(value = 1, message = "expectedVersion 必须大于 0")
        Long expectedVersion,

        @Size(max = 512, message = "商品标题不能超过 512 个字符")
        @Pattern(regexp = ".*\\S.*", message = "商品标题不能为空")
        String title,

        @Size(max = 128, message = "品牌不能超过 128 个字符")
        @Pattern(regexp = ".*\\S.*", message = "品牌不能为空")
        String brand,

        @Size(max = 255, message = "卖家不能超过 255 个字符")
        @Pattern(regexp = ".*\\S.*", message = "卖家不能为空")
        String seller,

        @Size(max = 128, message = "一级类目不能超过 128 个字符")
        @Pattern(regexp = ".*\\S.*", message = "一级类目不能为空")
        String categoryL1,

        @Size(max = 128, message = "二级类目不能超过 128 个字符")
        @Pattern(regexp = ".*\\S.*", message = "二级类目不能为空")
        String categoryL2,

        @Size(max = 128, message = "三级类目不能超过 128 个字符")
        @Pattern(regexp = ".*\\S.*", message = "三级类目不能为空")
        String categoryL3,

        @PositiveOrZero(message = "价格不能小于 0")
        Long snapshotPriceMinor,

        @Pattern(regexp = "[A-Z]{3}", message = "币种必须为 3 位大写代码")
        String currency,

        @Pattern(regexp = "verified|unverified|missing", message = "价格状态不合法")
        String priceStatus,

        @Size(max = 8000, message = "商品属性文本不能超过 8000 个字符")
        @Pattern(regexp = ".*\\S.*", message = "商品属性文本不能为空")
        String attributeText
) {
    public ProductUpdateRequest {
        title = strip(title);
        brand = strip(brand);
        seller = strip(seller);
        categoryL1 = strip(categoryL1);
        categoryL2 = strip(categoryL2);
        categoryL3 = strip(categoryL3);
        currency = strip(currency);
        priceStatus = strip(priceStatus);
        attributeText = strip(attributeText);
    }

    @AssertTrue(message = "至少提供一个可更新字段")
    public boolean isChanged() {
        return title != null || brand != null || seller != null
                || categoryL1 != null || categoryL2 != null || categoryL3 != null
                || snapshotPriceMinor != null || currency != null
                || priceStatus != null || attributeText != null;
    }

    private static String strip(String value) {
        return value == null ? null : value.strip();
    }
}

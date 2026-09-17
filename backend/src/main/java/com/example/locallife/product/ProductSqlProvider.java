package com.example.locallife.product;

import org.apache.ibatis.builder.annotation.ProviderContext;

import java.util.Map;

public final class ProductSqlProvider {

    private ProductSqlProvider() {
    }

    public static String findByFilters(Map<String, Object> parameters, ProviderContext ignored) {
        StringBuilder sql = new StringBuilder("SELECT ")
                .append(ProductMapper.COLUMNS)
                .append(" FROM product WHERE lifecycle_status = 'ACTIVE'");
        if (parameters.containsKey("catalogVersion") && parameters.get("catalogVersion") != null) {
            sql.append(" AND id IN (SELECT product_id FROM catalog_version_member WHERE catalog_version=#{catalogVersion})");
        }
        if (parameters.get("query") != null) {
            sql.append("""
                     AND (
                         LOWER(title) LIKE CONCAT('%', LOWER(#{query}), '%')
                         OR LOWER(COALESCE(attribute_text, '')) LIKE CONCAT('%', LOWER(#{query}), '%')
                     )
                    """);
        }
        if (parameters.get("category") != null) {
            sql.append("""
                     AND (
                         category_l1 = #{category}
                         OR category_l2 LIKE CONCAT('%', #{category}, '%')
                         OR category_l3 LIKE CONCAT('%', #{category}, '%')
                     )
                    """);
        }
        if (parameters.get("brand") != null) {
            sql.append(" AND LOWER(brand) = LOWER(#{brand})");
        }
        if (parameters.get("minPriceMinor") != null || parameters.get("maxPriceMinor") != null) {
            sql.append(" AND price_status = 'verified' AND snapshot_price_minor IS NOT NULL");
        }
        if (parameters.get("minPriceMinor") != null) {
            sql.append(" AND snapshot_price_minor >= #{minPriceMinor}");
        }
        if (parameters.get("maxPriceMinor") != null) {
            sql.append(" AND snapshot_price_minor <= #{maxPriceMinor}");
        }
        return sql.append(" ORDER BY id LIMIT #{limit}").toString();
    }
}

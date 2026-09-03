package com.example.locallife.shop;

import org.apache.ibatis.builder.annotation.ProviderContext;

import java.util.Map;

public final class ShopSqlProvider {

    private ShopSqlProvider() {
    }

    public static String findByFilters(Map<String, Object> parameters, ProviderContext ignored) {
        StringBuilder sql = new StringBuilder("SELECT ")
                .append(ShopMapper.COLUMNS)
                .append(" FROM shop WHERE 1=1");
        if (parameters.get("typeId") != null) {
            sql.append(" AND type_id = #{typeId}");
        }
        if (parameters.get("name") != null) {
            sql.append("""
                     AND (
                         LOWER(name) LIKE CONCAT('%', LOWER(#{name}), '%')
                         OR LOWER(COALESCE(original_name, '')) LIKE CONCAT('%', LOWER(#{name}), '%')
                     )
                    """);
        }
        return sql.append(" ORDER BY id").toString();
    }
}

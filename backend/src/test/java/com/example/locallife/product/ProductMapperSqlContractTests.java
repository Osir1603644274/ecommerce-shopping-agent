package com.example.locallife.product;

import org.apache.ibatis.annotations.Select;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Method;
import java.util.Arrays;

import static org.assertj.core.api.Assertions.assertThat;

class ProductMapperSqlContractTests {

    @Test
    void catalogPaginationSelectKeepsWhitespaceBeforeColumns() throws Exception {
        Method method = ProductMapper.class.getDeclaredMethod(
                "findByIdAfter", Long.class, int.class);
        String sql = String.join(" ", method.getAnnotation(Select.class).value());

        assertThat(sql).containsPattern("(?s)SELECT\\s+id,");
        assertThat(sql).doesNotContain("SELECTid");
    }
}

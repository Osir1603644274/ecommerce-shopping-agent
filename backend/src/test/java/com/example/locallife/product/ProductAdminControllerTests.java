package com.example.locallife.product;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.test.context.support.WithMockUser;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
@Transactional
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class ProductAdminControllerTests {
    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private JdbcTemplate jdbc;

    @Test
    @WithMockUser(roles = "ADMIN")
    void adminCreateStartsAtVersionOneAndWritesBoundOutboxEvent() throws Exception {
        mockMvc.perform(post("/api/admin/products")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "id":900001,
                                  "source":"admin-fixture",
                                  "sourceItemId":"admin-900001",
                                  "title":"可验证测试手机",
                                  "brand":"测试品牌",
                                  "seller":"测试商家",
                                  "categoryL1":"手机数码",
                                  "categoryL2":"手机",
                                  "categoryL3":"二手手机",
                                  "snapshotPriceMinor":199900,
                                  "currency":"CNY",
                                  "priceStatus":"verified",
                                  "attributeText":"12GB 256GB",
                                  "dataNature":"synthetic_test_fixture",
                                  "datasetRevision":"admin-v1",
                                  "sourceLicense":"internal-test",
                                  "provenanceUrl":"https://example.invalid/product/900001"
                                }
                                """))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.data.productId").value(900001))
                .andExpect(jsonPath("$.data.entityVersion").value(1))
                .andExpect(jsonPath("$.data.operation").value("UPSERT"));

        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM product WHERE id = 900001 AND lifecycle_status = 'ACTIVE'",
                Integer.class)).isEqualTo(1);
        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM outbox_event WHERE aggregate_type = 'PRODUCT' "
                        + "AND aggregate_id = '900001' AND event_type = 'product.search.upsert.v1'",
                Integer.class)).isEqualTo(1);
    }

    @Test
    @WithMockUser(roles = "USER")
    void ordinaryUserCannotCreateProduct() throws Exception {
        mockMvc.perform(post("/api/admin/products")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{}"))
                .andExpect(status().isForbidden());
    }

    @Test
    @WithMockUser(roles = "ADMIN")
    void adminUpdateAdvancesVersionAndWritesBoundOutboxEvent() throws Exception {
        mockMvc.perform(patch("/api/admin/products/1001")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"expectedVersion":1,"title":"远航 P1 旗舰版","snapshotPriceMinor":259900}
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.productId").value(1001))
                .andExpect(jsonPath("$.data.entityVersion").value(2))
                .andExpect(jsonPath("$.data.operation").value("UPSERT"))
                .andExpect(jsonPath("$.data.outboxEventId").isNotEmpty());

        assertThat(jdbc.queryForObject(
                "SELECT entity_version FROM product WHERE id = 1001", Long.class)).isEqualTo(2L);
        assertThat(jdbc.queryForObject(
                "SELECT event_type FROM outbox_event WHERE aggregate_type = 'PRODUCT' AND aggregate_id = '1001'",
                String.class)).isEqualTo("product.search.upsert.v1");
    }

    @Test
    @WithMockUser(roles = "ADMIN")
    void staleVersionIsRejectedWithoutMutationOrEvent() throws Exception {
        mockMvc.perform(patch("/api/admin/products/1001")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"expectedVersion\":99,\"title\":\"过期写入\"}"))
                .andExpect(status().isConflict());

        assertThat(jdbc.queryForObject(
                "SELECT entity_version FROM product WHERE id = 1001", Long.class)).isEqualTo(1L);
        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM outbox_event WHERE aggregate_type = 'PRODUCT' AND aggregate_id = '1001'",
                Integer.class)).isZero();
    }

    @Test
    @WithMockUser(roles = "ADMIN")
    void unknownMutationFieldFailsClosed() throws Exception {
        mockMvc.perform(patch("/api/admin/products/1001")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"expectedVersion\":1,\"inventory\":999}"))
                .andExpect(status().isBadRequest());
    }

    @Test
    @WithMockUser(roles = "USER")
    void ordinaryUserCannotMutateProduct() throws Exception {
        mockMvc.perform(delete("/api/admin/products/1001")
                        .param("expectedVersion", "1"))
                .andExpect(status().isForbidden());
    }

    @Test
    @WithMockUser(roles = "ADMIN")
    void adminDeleteIsVersionedSoftDeleteWithOutboxReceipt() throws Exception {
        mockMvc.perform(delete("/api/admin/products/1001")
                        .param("expectedVersion", "1"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.entityVersion").value(2))
                .andExpect(jsonPath("$.data.operation").value("DELETE"));

        assertThat(jdbc.queryForObject(
                "SELECT lifecycle_status FROM product WHERE id = 1001", String.class))
                .isEqualTo("DELETED");
        assertThat(jdbc.queryForObject(
                "SELECT event_type FROM outbox_event WHERE aggregate_type = 'PRODUCT' AND aggregate_id = '1001'",
                String.class)).isEqualTo("product.search.delete.v1");
    }
}

package com.example.locallife.ordering;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.UUID;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest
@AutoConfigureMockMvc
@Transactional
class OrderPageIntegrationTests {
    @Autowired OrderPageService pages;
    @Autowired OrderService orders;
    @Autowired JdbcTemplate jdbc;
    @Autowired MockMvc mvc;
    @MockitoSpyBean OrderMapper mapper;
    private String user;
    private final LocalDateTime time = LocalDateTime.of(2026, 9, 4, 12, 0);

    @BeforeEach void seed() {
        user = UUID.randomUUID().toString();
        jdbc.update("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES(?,?,'x',TRUE,0)", user, user);
        for (int i = 0; i < 45; i++) add(i, time, i % 2 == 0 ? "PAID" : "CANCELLED");
        clearInvocations(mapper);
    }

    private void add(int i, LocalDateTime date, String status) {
        String id = String.format("%036d", i);
        jdbc.update("""
            INSERT INTO customer_order(id,order_no,user_id,idempotency_key,request_hash,status,
            total_minor,discount_minor,payable_minor,currency,expires_at,version,created_at)
            VALUES(?,?,?,?,?,?,100,0,100,'CNY',?,0,?)
            """, id, "PAGE-" + i, user, "page-" + i, "hash", status, date.plusDays(1), date);
        jdbc.update("""
            INSERT INTO order_item(order_id,item_type,item_id,title_snapshot,unit_price_minor,quantity,subtotal_minor,evidence_json)
            VALUES(?,'PRODUCT',1001,'snapshot',100,1,100,'{}')
            """, id);
    }

    @Test void pageHasStableTiesAndUsesOnlyTwoMapperQueries() {
        OrderPage page = pages.page(user, 20, null, null);
        assertThat(page.orders()).hasSize(20).allSatisfy(o -> assertThat(o.items()).hasSize(1));
        assertThat(page.hasMore()).isTrue();
        verify(mapper).findPage(eq(user), isNull(), isNull(), isNull(), eq(21));
        verify(mapper).findItemsForOrders(argThat(ids -> ids.size() == 20));
        verify(mapper, never()).findItems(anyString());
    }

    @Test void completeTraversalHasNoDuplicatesAndNewHeadDoesNotShiftOldPages() {
        var collected = new ArrayList<String>();
        OrderPage page = pages.page(user, 7, null, null);
        collected.addAll(page.orders().stream().map(OrderResponse::id).toList());
        add(99, time.plusSeconds(1), "PAID");
        while (page.hasMore()) {
            page = pages.page(user, 7, null, page.nextCursor());
            collected.addAll(page.orders().stream().map(OrderResponse::id).toList());
        }
        assertThat(collected).hasSize(45).doesNotHaveDuplicates().isSortedAccordingTo(java.util.Comparator.reverseOrder());
        assertThat(page.nextCursor()).isNull();
    }

    @Test void cursorCannotCrossUsersFiltersOrBeTampered() {
        String cursor = pages.page(user, 5, "PAID", null).nextCursor();
        assertThatThrownBy(() -> pages.page(UUID.randomUUID().toString(), 5, "PAID", cursor)).hasMessageContaining("400");
        assertThatThrownBy(() -> pages.page(user, 5, "CANCELLED", cursor)).hasMessageContaining("400");
        assertThatThrownBy(() -> pages.page(user, 5, "PAID", "x" + cursor)).hasMessageContaining("400");
        assertThat(pages.page(user, 100, "PAID", null).orders()).hasSize(23);
        assertThatThrownBy(() -> pages.page(user, 101, null, null)).hasMessageContaining("400");
        assertThatThrownBy(() -> pages.page(user, 20, "INVALID", null)).hasMessageContaining("400");
    }

    @Test void emptyUserDoesNotQueryItemsAndLegacyResponseIsPreserved() {
        assertThat(pages.page("absent", 20, null, null).orders()).isEmpty();
        verify(mapper, never()).findItemsForOrders(anyList());
        var legacy = orders.listMine(user);
        assertThat(legacy).hasSize(45).allSatisfy(o -> assertThat(o.items()).hasSize(1));
        verify(mapper, never()).findItems(anyString());
    }

    @Test void httpPageRequiresAuthenticationAndValidatesBounds() throws Exception {
        mvc.perform(get("/api/orders/page")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/orders/page").with(jwt().jwt(j -> j.subject(user))))
                .andExpect(status().isOk()).andExpect(jsonPath("$.data.orders.length()").value(20))
                .andExpect(jsonPath("$.data.hasMore").value(true));
        mvc.perform(get("/api/orders/page?size=0").with(jwt().jwt(j -> j.subject(user))))
                .andExpect(status().isBadRequest());
    }

    @Test void fulfillmentReadRequiresOwnershipAndRecoveryRequiresAdmin() throws Exception {
        String orderId = String.format("%036d", 0);
        mvc.perform(get("/api/orders/" + orderId + "/fulfillment")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/orders/" + orderId + "/fulfillment").with(jwt().jwt(j -> j.subject("another-user"))))
                .andExpect(status().isForbidden());
        for (String route : new String[]{"/api/admin/fulfillment/" + orderId + "/retry",
                "/api/admin/fulfillment/events/example/replay"}) {
            mvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post(route)
                    .with(jwt().jwt(j -> j.subject(user))))
                    .andExpect(status().isForbidden());
        }
    }
}

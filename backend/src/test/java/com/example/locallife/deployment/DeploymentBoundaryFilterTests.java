package com.example.locallife.deployment;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

import static org.assertj.core.api.Assertions.assertThat;

class DeploymentBoundaryFilterTests {
    private static final String TOKEN = "0123456789abcdef0123456789abcdef";

    @Test
    void catalogRoleRejectsTradeRouteBeforeItCanExecute() throws Exception {
        var filter = filter("catalog", true);
        var request = new MockHttpServletRequest("POST", "/api/orders");
        var response = new MockHttpServletResponse();

        filter.doFilter(request, response, new MockFilterChain());

        assertThat(response.getStatus()).isEqualTo(404);
        assertThat(response.getContentAsString()).contains("不拥有此接口");
    }

    @Test
    void internalCatalogContractRequiresToken() throws Exception {
        var filter = filter("catalog", true);
        var denied = new MockHttpServletResponse();
        filter.doFilter(
                new MockHttpServletRequest("GET", "/internal/trade-catalog/items/PRODUCT/1"),
                denied,
                new MockFilterChain()
        );
        assertThat(denied.getStatus()).isEqualTo(403);

        var allowedRequest = new MockHttpServletRequest(
                "GET", "/internal/trade-catalog/items/PRODUCT/1");
        allowedRequest.addHeader("X-Internal-Service-Token", TOKEN);
        var allowed = new MockHttpServletResponse();
        var chain = new MockFilterChain();
        filter.doFilter(allowedRequest, allowed, chain);

        assertThat(allowed.getStatus()).isEqualTo(200);
        assertThat(chain.getRequest()).isNotNull();
    }

    @Test
    void tradeRoleRejectsCatalogRouteButAllowsOrders() throws Exception {
        var filter = filter("trade", true);
        var denied = new MockHttpServletResponse();
        filter.doFilter(new MockHttpServletRequest("GET", "/api/products/1"),
                denied, new MockFilterChain());
        assertThat(denied.getStatus()).isEqualTo(404);

        var chain = new MockFilterChain();
        filter.doFilter(new MockHttpServletRequest("POST", "/api/orders"),
                new MockHttpServletResponse(), chain);
        assertThat(chain.getRequest()).isNotNull();
    }

    @Test
    void catalogRoleRetainsReviewRecommendationAndBehaviorSurfaces() throws Exception {
        var filter = filter("catalog", true);

        for (String path : new String[]{
                "/api/reviews/shop/1", "/api/recommendations/shops",
                "/api/user-behaviors"
        }) {
            var chain = new MockFilterChain();
            filter.doFilter(new MockHttpServletRequest("GET", path),
                    new MockHttpServletResponse(), chain);
            assertThat(chain.getRequest()).as(path).isNotNull();
        }
    }

    private static DeploymentBoundaryFilter filter(String role, boolean requireToken) {
        return new DeploymentBoundaryFilter(
                new DeploymentProperties(role, requireToken, TOKEN),
                new ObjectMapper().findAndRegisterModules()
        );
    }
}

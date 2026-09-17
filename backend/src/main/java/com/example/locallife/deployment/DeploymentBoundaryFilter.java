package com.example.locallife.deployment;

import com.example.locallife.common.ApiResponse;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;

@Component
@Order(Ordered.HIGHEST_PRECEDENCE + 10)
class DeploymentBoundaryFilter extends OncePerRequestFilter {
    private static final List<String> CATALOG_PATHS = List.of(
            "/api/products", "/api/admin/products", "/api/shops", "/api/shop-types",
            "/api/reviews", "/api/recommendations", "/api/user-behaviors", "/api/product-favorites",
            "/internal/catalog", "/internal/trade-catalog"
    );
    private static final List<String> TRADE_PATHS = List.of(
            "/api/auth", "/api/identity", "/api/orders", "/api/inventory",
            "/api/payments", "/api/flash-sales", "/api/coupons", "/api/memory", "/api/admin/fulfillment"
    );

    private final DeploymentProperties properties;
    private final ObjectMapper json;

    DeploymentBoundaryFilter(DeploymentProperties properties, ObjectMapper json) {
        this.properties = properties;
        this.json = json;
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain
    ) throws ServletException, IOException {
        String path = request.getRequestURI();
        response.setHeader("X-Service-Role",properties.role());
        response.setHeader("X-Service-Instance",System.getenv().getOrDefault("SERVICE_INSTANCE_ID","local"));
        if (!roleAllows(path)) {
            write(response, HttpServletResponse.SC_NOT_FOUND, "该服务不拥有此接口");
            return;
        }
        if (path.startsWith("/internal/") && properties.requireInternalToken()
                && !secureEquals(request.getHeader("X-Internal-Service-Token"),
                properties.internalToken())) {
            write(response, HttpServletResponse.SC_FORBIDDEN, "内部服务凭证无效");
            return;
        }
        filterChain.doFilter(request, response);
    }

    private boolean roleAllows(String path) {
        if ("monolith".equals(properties.role()) || commonPath(path)) {
            return true;
        }
        return switch (properties.role()) {
            case "catalog" -> hasPrefix(path, CATALOG_PATHS);
            case "trade" -> hasPrefix(path, TRADE_PATHS);
            default -> false;
        };
    }

    private static boolean commonPath(String path) {
        return path.startsWith("/actuator/") || "/actuator".equals(path)
                || "/api/health".equals(path) || "/error".equals(path)
                || path.startsWith("/api/diagnostics/backend-traces/");
    }

    private static boolean hasPrefix(String path, List<String> prefixes) {
        return prefixes.stream().anyMatch(prefix ->
                path.equals(prefix) || path.startsWith(prefix + "/"));
    }

    private static boolean secureEquals(String supplied, String expected) {
        if (supplied == null) {
            return false;
        }
        return MessageDigest.isEqual(
                supplied.getBytes(StandardCharsets.UTF_8),
                expected.getBytes(StandardCharsets.UTF_8)
        );
    }

    private void write(HttpServletResponse response, int status, String message) throws IOException {
        response.setStatus(status);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        json.writeValue(response.getOutputStream(), ApiResponse.fail(message));
    }
}

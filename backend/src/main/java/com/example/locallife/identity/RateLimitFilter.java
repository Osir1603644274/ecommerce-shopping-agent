package com.example.locallife.identity;

import com.example.locallife.common.ApiResponse;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.MediaType;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashSet;
import java.util.Set;

@Component
@ConditionalOnProperty(
        name = "local-life.rate-limit.enabled",
        havingValue = "true",
        matchIfMissing = true
)
class RateLimitFilter extends OncePerRequestFilter {

    private final SlidingWindowRateLimiter rateLimiter;
    private final RateLimitProperties properties;
    private final ObjectMapper objectMapper;

    RateLimitFilter(
            SlidingWindowRateLimiter rateLimiter,
            RateLimitProperties properties,
            ObjectMapper objectMapper
    ) {
        this.rateLimiter = rateLimiter;
        this.properties = properties;
        this.objectMapper = objectMapper;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        String method = request.getMethod();
        if ("GET".equals(method) || "HEAD".equals(method) || "OPTIONS".equals(method)) {
            return true;
        }
        return !request.getRequestURI().startsWith("/api/");
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain
    ) throws ServletException, IOException {
        boolean authEndpoint = request.getRequestURI().startsWith("/api/auth/");
        int limit = authEndpoint ? properties.authLimit() : properties.writeLimit();
        Set<String> dimensions = dimensions(request, authEndpoint);
        long retryAfterSeconds = 0;
        boolean allowed = true;
        for (String dimension : dimensions) {
            SlidingWindowRateLimiter.Decision decision =
                    rateLimiter.acquire(dimension, limit, properties.window());
            if (!decision.allowed()) {
                allowed = false;
                retryAfterSeconds = Math.max(retryAfterSeconds, decision.retryAfterSeconds());
            }
        }
        if (allowed) {
            filterChain.doFilter(request, response);
            return;
        }

        response.setStatus(429);
        response.setHeader("Retry-After", Long.toString(Math.max(1, retryAfterSeconds)));
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        objectMapper.writeValue(response.getOutputStream(), ApiResponse.fail("请求过于频繁，请稍后重试"));
    }

    private static String authenticatedSubject(HttpServletRequest request) {
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication != null && authentication.isAuthenticated()) {
            return authentication.getName();
        }
        return request.getRemoteAddr();
    }

    private static Set<String> dimensions(HttpServletRequest request, boolean authEndpoint) {
        String route = normalizedRoute(request.getRequestURI());
        String method = request.getMethod();
        String remoteAddress = request.getRemoteAddr();
        Set<String> dimensions = new LinkedHashSet<>();
        dimensions.add((authEndpoint ? "auth-ip:" : "write-ip:")
                + remoteAddress + ":" + method + ":" + route);
        if (!authEndpoint) {
            dimensions.add("write-user:" + authenticatedSubject(request)
                    + ":" + method + ":" + route);
        }
        return dimensions;
    }

    private static String normalizedRoute(String uri) {
        return uri
                .replaceAll("/[0-9]+(?=/|$)", "/:id")
                .replaceAll("/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}(?=/|$)", "/:id");
    }
}

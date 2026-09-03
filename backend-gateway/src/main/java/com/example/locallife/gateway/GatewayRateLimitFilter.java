package com.example.locallife.gateway;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.core.Ordered;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.security.config.web.server.SecurityWebFiltersOrder;
import org.springframework.stereotype.Component;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.WebFilter;
import org.springframework.web.server.WebFilterChain;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.nio.charset.StandardCharsets;
import java.security.Principal;
import java.util.LinkedHashSet;
import java.util.Set;

@Component
@ConditionalOnProperty(name = "gateway.rate-limit.enabled", havingValue = "true")
class GatewayRateLimitFilter implements WebFilter, Ordered {
    private final ReactiveSlidingWindowRateLimiter limiter;
    private final GatewayRateLimitProperties properties;

    GatewayRateLimitFilter(
            ReactiveSlidingWindowRateLimiter limiter,
            GatewayRateLimitProperties properties
    ) {
        this.limiter = limiter;
        this.properties = properties;
    }

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        HttpMethod method = exchange.getRequest().getMethod();
        String path = exchange.getRequest().getPath().value();
        if (method == null || method == HttpMethod.GET || method == HttpMethod.HEAD
                || method == HttpMethod.OPTIONS || !path.startsWith("/api/")) {
            return chain.filter(exchange);
        }
        boolean auth = path.startsWith("/api/auth/");
        int limit = auth ? properties.authLimit() : properties.writeLimit();
        return exchange.getPrincipal()
                .map(Principal::getName)
                .defaultIfEmpty("anonymous")
                .flatMap(subject -> evaluate(exchange, chain, path, method.name(),
                        subject, auth, limit));
    }

    private Mono<Void> evaluate(
            ServerWebExchange exchange,
            WebFilterChain chain,
            String path,
            String method,
            String subject,
            boolean auth,
            int limit
    ) {
        String ip = exchange.getRequest().getRemoteAddress() == null
                ? "unknown" : exchange.getRequest().getRemoteAddress().getAddress().getHostAddress();
        String route = normalizedRoute(path);
        Set<String> dimensions = new LinkedHashSet<>();
        dimensions.add((auth ? "auth-ip:" : "write-ip:") + ip + ":" + method + ":" + route);
        if (!auth) {
            dimensions.add("write-user:" + subject + ":" + method + ":" + route);
        }
        return Flux.fromIterable(dimensions)
                .concatMap(dimension -> limiter.acquire(
                        dimension, limit, properties.window()))
                .collectList()
                .flatMap(decisions -> {
                    long retryAfter = decisions.stream()
                            .filter(decision -> !decision.allowed())
                            .mapToLong(ReactiveSlidingWindowRateLimiter.Decision::retryAfterSeconds)
                            .max().orElse(0);
                    return retryAfter == 0
                            ? chain.filter(exchange) : reject(exchange, retryAfter);
                });
    }

    private static Mono<Void> reject(ServerWebExchange exchange, long retryAfter) {
        exchange.getResponse().setStatusCode(HttpStatus.TOO_MANY_REQUESTS);
        exchange.getResponse().getHeaders().setContentType(MediaType.APPLICATION_JSON);
        exchange.getResponse().getHeaders().set("Retry-After", Long.toString(retryAfter));
        byte[] bytes = "{\"success\":false,\"data\":null,\"message\":\"请求过于频繁，请稍后重试\"}"
                .getBytes(StandardCharsets.UTF_8);
        return exchange.getResponse().writeWith(Mono.just(
                exchange.getResponse().bufferFactory().wrap(bytes)));
    }

    private static String normalizedRoute(String uri) {
        return uri
                .replaceAll("/[0-9]+(?=/|$)", "/:id")
                .replaceAll("/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}(?=/|$)", "/:id");
    }

    @Override
    public int getOrder() {
        return SecurityWebFiltersOrder.LAST.getOrder() + 1;
    }
}

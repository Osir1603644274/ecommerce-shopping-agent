package com.example.locallife.gateway;

import com.nimbusds.jose.jwk.source.ImmutableSecret;
import com.nimbusds.jose.proc.SecurityContext;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.reactive.EnableWebFluxSecurity;
import org.springframework.security.config.web.server.ServerHttpSecurity;
import org.springframework.security.oauth2.core.DelegatingOAuth2TokenValidator;
import org.springframework.security.oauth2.core.OAuth2Error;
import org.springframework.security.oauth2.core.OAuth2TokenValidator;
import org.springframework.security.oauth2.core.OAuth2TokenValidatorResult;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.jwt.JwtValidators;
import org.springframework.security.oauth2.jwt.NimbusReactiveJwtDecoder;
import org.springframework.security.oauth2.jwt.ReactiveJwtDecoder;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationConverter;
import org.springframework.security.oauth2.server.resource.authentication.JwtGrantedAuthoritiesConverter;
import org.springframework.security.oauth2.server.resource.authentication.ReactiveJwtAuthenticationConverterAdapter;
import org.springframework.security.web.server.SecurityWebFilterChain;
import reactor.core.publisher.Mono;

import javax.crypto.SecretKey;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;

@Configuration
@EnableWebFluxSecurity
@EnableConfigurationProperties({GatewayAuthProperties.class, GatewayRateLimitProperties.class})
class GatewaySecurityConfiguration {
    @Bean
    ReactiveJwtDecoder gatewayJwtDecoder(GatewayAuthProperties properties) {
        SecretKey key = new SecretKeySpec(
                properties.secret().getBytes(StandardCharsets.UTF_8), "HmacSHA256");
        NimbusReactiveJwtDecoder decoder = NimbusReactiveJwtDecoder.withSecretKey(key)
                .macAlgorithm(MacAlgorithm.HS256)
                .build();
        OAuth2TokenValidator<Jwt> tokenType = token ->
                "access".equals(token.getClaimAsString("type"))
                        ? OAuth2TokenValidatorResult.success()
                        : OAuth2TokenValidatorResult.failure(new OAuth2Error(
                        "invalid_token", "Bearer token is not an access token", null));
        decoder.setJwtValidator(new DelegatingOAuth2TokenValidator<>(
                JwtValidators.createDefaultWithIssuer(properties.issuer()), tokenType));
        return decoder;
    }

    @Bean
    SecurityWebFilterChain gatewaySecurity(
            ServerHttpSecurity http,
            ReactiveJwtDecoder gatewayJwtDecoder
    ) {
        return http
                .csrf(ServerHttpSecurity.CsrfSpec::disable)
                .httpBasic(ServerHttpSecurity.HttpBasicSpec::disable)
                .formLogin(ServerHttpSecurity.FormLoginSpec::disable)
                .authorizeExchange(authorize -> authorize
                        .pathMatchers("/actuator/health", "/actuator/health/**").permitAll()
                        .pathMatchers("/api/auth/**", "/api/health").permitAll()
                        .pathMatchers(HttpMethod.POST, "/api/payments/callbacks/**").permitAll()
                        .pathMatchers(HttpMethod.GET,
                                "/api/products/**", "/api/shops/**", "/api/shop-types/**",
                                "/api/reviews/**", "/api/recommendations/**")
                        .permitAll()
                        .pathMatchers(HttpMethod.POST, "/api/products/resolve", "/api/products/resolve-sources").permitAll()
                        .pathMatchers(HttpMethod.POST, "/api/admin/products").hasRole("ADMIN")
                        .pathMatchers(HttpMethod.PATCH, "/api/admin/products/**").hasRole("ADMIN")
                        .pathMatchers(HttpMethod.DELETE, "/api/admin/products/**").hasRole("ADMIN")
                        .pathMatchers(HttpMethod.PUT, "/api/shops/**").hasRole("ADMIN")
                        .pathMatchers("/api/**").authenticated()
                        .anyExchange().denyAll())
                .oauth2ResourceServer(resource -> resource
                        .jwt(jwt -> jwt
                                .jwtDecoder(gatewayJwtDecoder)
                                .jwtAuthenticationConverter(jwtConverter()))
                        .authenticationEntryPoint((exchange, exception) ->
                                writeError(exchange, HttpStatus.UNAUTHORIZED, "需要有效的访问令牌")))
                .exceptionHandling(exceptions -> exceptions
                        .authenticationEntryPoint((exchange, exception) ->
                                writeError(exchange, HttpStatus.UNAUTHORIZED, "需要登录"))
                        .accessDeniedHandler((exchange, exception) ->
                                writeError(exchange, HttpStatus.FORBIDDEN, "无权执行此操作")))
                .build();
    }

    private static ReactiveJwtAuthenticationConverterAdapter jwtConverter() {
        JwtGrantedAuthoritiesConverter authorities = new JwtGrantedAuthoritiesConverter();
        authorities.setAuthoritiesClaimName("roles");
        authorities.setAuthorityPrefix("ROLE_");
        JwtAuthenticationConverter delegate = new JwtAuthenticationConverter();
        delegate.setJwtGrantedAuthoritiesConverter(authorities);
        return new ReactiveJwtAuthenticationConverterAdapter(delegate);
    }

    private static Mono<Void> writeError(
            org.springframework.web.server.ServerWebExchange exchange,
            HttpStatus status,
            String message
    ) {
        exchange.getResponse().setStatusCode(status);
        exchange.getResponse().getHeaders().setContentType(MediaType.APPLICATION_JSON);
        byte[] bytes = ("{\"success\":false,\"data\":null,\"message\":\""
                + message + "\"}").getBytes(StandardCharsets.UTF_8);
        DataBuffer buffer = exchange.getResponse().bufferFactory().wrap(bytes);
        return exchange.getResponse().writeWith(Mono.just(buffer));
    }
}

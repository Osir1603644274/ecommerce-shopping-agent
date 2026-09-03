package com.example.locallife.identity;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.FilterChain;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;

import java.time.Duration;
import java.util.List;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class RateLimitFilterTests {

    @AfterEach
    void clearSecurityContext() {
        SecurityContextHolder.clearContext();
    }

    @Test
    void writeRequestUsesBothIpAndUserDimensionsWithNormalizedResourcePath() throws Exception {
        SlidingWindowRateLimiter limiter = mock(SlidingWindowRateLimiter.class);
        when(limiter.acquire(
                org.mockito.ArgumentMatchers.anyString(),
                org.mockito.ArgumentMatchers.anyInt(),
                org.mockito.ArgumentMatchers.any(Duration.class)
        )).thenReturn(new SlidingWindowRateLimiter.Decision(true, 0));
        RateLimitFilter filter = new RateLimitFilter(
                limiter,
                new RateLimitProperties(true, 20, 120, Duration.ofMinutes(1)),
                new ObjectMapper()
        );
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(
                        "user-1", "", List.of(new SimpleGrantedAuthority("ROLE_USER"))
                )
        );
        MockHttpServletRequest request = new MockHttpServletRequest("POST", "/api/orders/123");
        request.setRemoteAddr("10.0.0.7");
        MockHttpServletResponse response = new MockHttpServletResponse();
        FilterChain chain = mock(FilterChain.class);

        filter.doFilter(request, response, chain);

        verify(limiter).acquire(
                "write-ip:10.0.0.7:POST:/api/orders/:id", 120, Duration.ofMinutes(1));
        verify(limiter).acquire(
                "write-user:user-1:POST:/api/orders/:id", 120, Duration.ofMinutes(1));
        verify(chain).doFilter(request, response);
    }
}

package com.example.inventoryservice;

import jakarta.servlet.*;
import jakarta.servlet.http.*;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

@Component
public class InternalIdentityFilter extends OncePerRequestFilter {
    private final String readToken,writeToken;
    public InternalIdentityFilter(@Value("${inventory.read-token}") String readToken,
                                  @Value("${inventory.write-token}") String writeToken) {
        if(readToken.length()<32 || writeToken.length()<32 || readToken.equals(writeToken))
            throw new IllegalArgumentException("distinct_inventory_service_credentials_required");
        this.readToken=readToken;this.writeToken=writeToken;
    }
    private boolean matches(String left,String right) {
        return left!=null && MessageDigest.isEqual(left.getBytes(StandardCharsets.UTF_8),right.getBytes(StandardCharsets.UTF_8));
    }
    @Override protected void doFilterInternal(HttpServletRequest request,HttpServletResponse response,FilterChain chain)
            throws ServletException,IOException {
        String path=request.getRequestURI();
        if(path.equals("/actuator/health")){chain.doFilter(request,response);return;}
        String supplied=request.getHeader("X-Inventory-Service-Token");
        boolean readStock=("GET".equals(request.getMethod()) && path.startsWith("/internal/inventory/stocks/"))
            || ("POST".equals(request.getMethod()) && path.equals("/internal/inventory/stocks/query"));
        if(!matches(supplied,writeToken) && !(readStock && matches(supplied,readToken))) {
            response.setStatus(403);response.setContentType("application/json");
            response.getWriter().write("{\"error\":\"inventory_service_identity_required\"}");return;
        }
        if(request.getContentLengthLong()>65536){response.setStatus(413);return;}
        chain.doFilter(request,response);
    }
}

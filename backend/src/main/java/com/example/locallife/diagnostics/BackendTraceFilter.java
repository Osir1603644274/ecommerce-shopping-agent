package com.example.locallife.diagnostics;

import jakarta.servlet.*;
import jakarta.servlet.http.*;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

@Component @Order(Ordered.HIGHEST_PRECEDENCE+20)
@ConditionalOnProperty(name="local-life.observer.enabled",havingValue="true")
public class BackendTraceFilter extends OncePerRequestFilter {
    public static final String KEY_HEADER="X-Backend-Observer-Key";
    private final BackendTraceStore store;
    private final String key;
    public BackendTraceFilter(BackendTraceStore store,@Value("${local-life.observer.key:}") String key) { this.store=store; this.key=key; }
    public boolean authorized(HttpServletRequest request) {
        var value=request.getHeader(KEY_HEADER);
        return key.length()>=32 && value!=null && value.length()<=256 &&
            MessageDigest.isEqual(key.getBytes(StandardCharsets.UTF_8),value.getBytes(StandardCharsets.UTF_8));
    }
    protected void doFilterInternal(HttpServletRequest req,HttpServletResponse res,FilterChain chain) throws ServletException,IOException {
        if(!authorized(req) || !req.getRequestURI().startsWith("/api/") || req.getRequestURI().startsWith("/api/diagnostics/")) {
            chain.doFilter(req,res); return;
        }
        // No query string or arbitrary path identifier is exposed.
        String path=req.getRequestURI().replaceAll("/[0-9a-fA-F-]{8,}(?=/|$)","/:id").replaceAll("/\\d+(?=/|$)","/:id");
        var context=BackendTrace.begin(req.getMethod(),path);
        res.setHeader("X-Java-Trace-Id",context.id);
        var event=BackendTrace.start("http",req.getMethod()+" "+path);
        boolean failed=false;
        try { chain.doFilter(req,res); }
        catch(IOException|ServletException|RuntimeException|Error ex) { failed=true; throw ex; }
        finally {
            context.httpStatus=failed?500:res.getStatus();
            BackendTrace.finish(event,failed?"exception":"HTTP "+res.getStatus(),null);
            try { store.save(context); } catch(RuntimeException ignored) { /* diagnostics must not fail business */ }
            finally { BackendTrace.clear(); }
        }
    }
}

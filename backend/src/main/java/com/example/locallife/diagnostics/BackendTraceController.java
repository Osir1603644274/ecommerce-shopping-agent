package com.example.locallife.diagnostics;

import com.example.locallife.common.ApiResponse;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.http.HttpStatus;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;

@RestController @RequestMapping("/api/diagnostics/backend-traces")
@ConditionalOnProperty(name="local-life.observer.enabled",havingValue="true")
public class BackendTraceController {
    private final BackendTraceStore store; private final BackendTraceFilter guard;
    public BackendTraceController(BackendTraceStore store,BackendTraceFilter guard) { this.store=store; this.guard=guard; }
    @GetMapping("/{id}")
    public ApiResponse<BackendTrace.Context> get(@PathVariable String id,HttpServletRequest request) {
        if(!guard.authorized(request)) throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        var trace=store.get(id);
        if(trace==null) throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        return ApiResponse.ok(trace);
    }
}

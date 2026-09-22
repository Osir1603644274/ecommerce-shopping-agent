package com.example.locallife.support;

import com.example.locallife.common.*;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import java.util.List;

@RestController
public class SupportTicketController {
    private final SupportTickets tickets;
    public SupportTicketController(SupportTickets tickets) {this.tickets=tickets;}
    public record Reply(long expectedVersion,String message) { }
    public record Action(long expectedVersion,String action,String message) { }
    @PostMapping("/api/support/tickets")
    public ApiResponse<SupportTickets.Ticket> create(@RequestBody SupportTickets.Request body,@RequestHeader("Idempotency-Key") String key,Authentication auth) {
        return ApiResponse.ok(tickets.create(body,auth.getName(),key));
    }
    @GetMapping("/api/support/tickets")
    public ApiResponse<List<SupportTickets.Ticket>> list(@RequestParam(defaultValue="0") int offset,@RequestParam(defaultValue="20") int limit,Authentication auth) {
        return ApiResponse.ok(tickets.list(auth.getName(),offset,limit));
    }
    @GetMapping("/api/support/tickets/{id}")
    public ApiResponse<SupportTickets.Ticket> get(@PathVariable String id,Authentication auth) {return ApiResponse.ok(tickets.get(id,auth.getName()));}
    @GetMapping("/api/support/tickets/{id}/events")
    public ApiResponse<List<SupportTickets.Event>> events(@PathVariable String id,Authentication auth) {return ApiResponse.ok(tickets.events(id,auth.getName()));}
    @PostMapping("/api/support/tickets/{id}/reply")
    public ApiResponse<SupportTickets.Ticket> reply(@PathVariable String id,@RequestBody Reply body,@RequestHeader("Idempotency-Key") String key,Authentication auth) {
        return ApiResponse.ok(tickets.reply(id,auth.getName(),body.expectedVersion(),body.message(),key));
    }
    @PostMapping("/api/admin/support-simulator/tickets/{id}/actions")
    public ApiResponse<SupportTickets.Ticket> action(@PathVariable String id,@RequestBody Action body,@RequestHeader("Idempotency-Key") String key,Authentication auth) {
        if(auth==null || auth.getAuthorities().stream().noneMatch(a->"ROLE_ADMIN".equals(a.getAuthority()))) throw new ForbiddenOperationException("需要工单管理员权限");
        return ApiResponse.ok(tickets.administer(id,auth.getName(),body.expectedVersion(),body.action(),body.message(),key));
    }
}

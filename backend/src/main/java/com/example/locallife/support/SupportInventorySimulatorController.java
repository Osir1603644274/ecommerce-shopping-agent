package com.example.locallife.support;

import com.example.locallife.common.*;
import com.example.locallife.inventory.remote.InventoryCommandDelivery;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import java.util.List;
import java.util.Map;

/** Independent simulator can retry persisted commands, never invent their outcomes. */
@RestController
@RequestMapping("/api/admin/support-simulator")
@ConditionalOnProperty(name="local-life.inventory.remote-enabled",havingValue="true")
public class SupportInventorySimulatorController {
    private final JdbcTemplate jdbc;
    private final InventoryCommandDelivery delivery;
    private final boolean enabled;

    public SupportInventorySimulatorController(JdbcTemplate jdbc,InventoryCommandDelivery delivery,
            @Value("${local-life.support.simulator-enabled:false}") boolean enabled) {
        this.jdbc=jdbc;this.delivery=delivery;this.enabled=enabled;
    }

    public record Retry(String commandId) { }

    @PostMapping("/orders/{id}/inventory-retry")
    public ApiResponse<Map<String,Object>> retryOrder(@PathVariable String id,@RequestBody Retry body,Authentication auth) {
        requireAdmin(auth);
        var rows=jdbc.queryForList("""
            SELECT j.command_id,j.order_id,j.status FROM inventory_command_journal j
            JOIN customer_order o ON o.id=j.order_id WHERE o.id=? AND j.command_id=?
            """,id,body.commandId());
        return retry(rows);
    }

    @PostMapping("/cases/{id}/inventory-retry")
    public ApiResponse<Map<String,Object>> retryCase(@PathVariable String id,@RequestBody Retry body,Authentication auth) {
        requireAdmin(auth);
        // An after-sale may only drive its own return effect or replacement identity.
        var rows=jdbc.queryForList("""
            SELECT j.command_id,j.order_id,j.status FROM inventory_command_journal j
            WHERE j.command_id=? AND (
              EXISTS(SELECT 1 FROM support_stock_effect e WHERE e.case_id=? AND e.effect_id=j.command_id)
              OR EXISTS(SELECT 1 FROM support_replacement r WHERE r.case_id=? AND r.id=j.order_id))
            """,body.commandId(),id,id);
        return retry(rows);
    }

    private ApiResponse<Map<String,Object>> retry(List<Map<String,Object>> rows) {
        if(rows.size()!=1) throw new ResourceNotFoundException("关联库存命令不存在");
        var row=rows.get(0);String command=(String)row.get("command_id");
        if("TRY".equals(row.get("status"))) delivery.reconcileTry(command,(String)row.get("order_id"));
        else delivery.deliver(command);
        return ApiResponse.ok(jdbc.queryForMap("""
            SELECT command_id,order_id,status,attempts,last_error FROM inventory_command_journal WHERE command_id=?
            """,command));
    }

    private void requireAdmin(Authentication auth) {
        if(!enabled || auth==null || auth.getAuthorities().stream().noneMatch(a->"ROLE_ADMIN".equals(a.getAuthority())))
            throw new ForbiddenOperationException("需要启用模拟器并具有管理员权限");
    }
}

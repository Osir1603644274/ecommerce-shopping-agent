package com.example.locallife.support;

import com.example.locallife.common.*;
import com.example.locallife.inventory.remote.InventoryCommandDelivery;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import java.util.List;
import java.util.Map;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.mockito.ArgumentMatchers.*;

class SupportInventorySimulatorControllerTests {
    final JdbcTemplate jdbc=mock(JdbcTemplate.class);
    final InventoryCommandDelivery delivery=mock(InventoryCommandDelivery.class);
    final UsernamePasswordAuthenticationToken admin=new UsernamePasswordAuthenticationToken("operator","unused",List.of(new SimpleGrantedAuthority("ROLE_ADMIN")));

    @Test void disabledOrCustomerCannotDriveInventory() {
        var request=new SupportInventorySimulatorController.Retry("existing");
        assertThatThrownBy(()->new SupportInventorySimulatorController(jdbc,delivery,false).retryOrder("order",request,admin)).isInstanceOf(ForbiddenOperationException.class);
        var customer=new UsernamePasswordAuthenticationToken("customer","unused",List.of(new SimpleGrantedAuthority("ROLE_USER")));
        assertThatThrownBy(()->new SupportInventorySimulatorController(jdbc,delivery,true).retryCase("case",request,customer)).isInstanceOf(ForbiddenOperationException.class);
        verifyNoInteractions(jdbc,delivery);
    }

    @Test void unassociatedCommandCannotBeDelivered() {
        when(jdbc.queryForList(anyString(),eq("foreign-command"),eq("case"),eq("case"))).thenReturn(List.of());
        assertThatThrownBy(()->new SupportInventorySimulatorController(jdbc,delivery,true).retryCase("case",new SupportInventorySimulatorController.Retry("foreign-command"),admin)).isInstanceOf(ResourceNotFoundException.class);
        verifyNoInteractions(delivery);
    }

    @Test void unresolvedDeliveryReportsPersistedPendingInsteadOfSuccess() {
        when(jdbc.queryForList(anyString(),eq("return:case"),eq("case"),eq("case"))).thenReturn(List.of(Map.of("command_id","return:case","order_id","order","status","PENDING")));
        when(delivery.deliver("return:case")).thenReturn(false);
        when(jdbc.queryForMap(anyString(),eq("return:case"))).thenReturn(Map.of("status","PENDING","attempts",1));
        var result=new SupportInventorySimulatorController(jdbc,delivery,true).retryCase("case",new SupportInventorySimulatorController.Retry("return:case"),admin);
        assertThat(result.data().get("status")).isEqualTo("PENDING");
        verify(delivery).deliver("return:case");verifyNoMoreInteractions(delivery);
    }

    @Test void reservationTryUsesOrderCommitReconciliation() {
        when(jdbc.queryForList(anyString(),eq("order"),eq("reserve:order"))).thenReturn(List.of(Map.of("command_id","reserve:order","order_id","order","status","TRY")));
        when(jdbc.queryForMap(anyString(),eq("reserve:order"))).thenReturn(Map.of("status","ACK"));
        new SupportInventorySimulatorController(jdbc,delivery,true).retryOrder("order",new SupportInventorySimulatorController.Retry("reserve:order"),admin);
        verify(delivery).reconcileTry("reserve:order","order");verifyNoMoreInteractions(delivery);
    }
}

package com.example.locallife.integration;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.dao.DuplicateKeyException;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

class OutboxPayloadIdentityTests {
    private OutboxService duplicate(String stored){
        OutboxMapper mapper=mock(OutboxMapper.class);
        doThrow(new DuplicateKeyException("duplicate")).when(mapper).insert(any());
        when(mapper.findById(anyString())).thenReturn(new OutboxEvent("id","INVENTORY","order","inventory.command.requested.v1",
            stored,"PENDING",0,null,null,null,null,null,null));
        return new OutboxService(mapper,new ObjectMapper());
    }
    @Test void mysqlJsonWhitespaceAndKeyOrderDoNotChangeIdempotency(){
        var service=duplicate("{\"version\": 1, \"commandId\": \"release:order\"}");
        assertThatCode(()->service.appendIdempotent("same","INVENTORY","order","inventory.command.requested.v1",
            java.util.Map.of("commandId","release:order","version",1))).doesNotThrowAnyException();
    }
    @Test void changedContentsStillConflict(){
        var service=duplicate("{\"commandId\": \"release:other\"}");
        assertThatThrownBy(()->service.appendIdempotent("same","INVENTORY","order","inventory.command.requested.v1",
            java.util.Map.of("commandId","release:order"))).isInstanceOf(IllegalStateException.class);
    }
}

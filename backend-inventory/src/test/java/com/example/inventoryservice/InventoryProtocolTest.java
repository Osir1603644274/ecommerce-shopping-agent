package com.example.inventoryservice;

import org.junit.jupiter.api.Test;
import java.time.LocalDateTime;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;
import static com.example.inventoryservice.InventoryProtocol.*;

class InventoryProtocolTest {
    @Test void payloadHashStableAcrossOrderingButBindsOrderQuantityKindAndDeadline(){
        var first=new Item("PRODUCT",1L,2);var second=new Item("PRODUCT",2L,1);
        var expiry=LocalDateTime.of(2026,9,15,12,0);
        var left=new Command("key","order","RESERVE",List.of(first,second),expiry);
        assertEquals(left.hash(),new Command("retry-key","order","RESERVE",List.of(second,first),expiry).hash());
        assertNotEquals(left.hash(),new Command("key","another","RESERVE",left.items(),expiry).hash());
        assertNotEquals(left.hash(),new Command("key","order","RESERVE",left.items(),expiry.plusSeconds(1)).hash());
    }
    @Test void rejectsAmbiguousBodies(){
        var item=new Item("PRODUCT",1L,1);
        assertThrows(IllegalArgumentException.class,()->new Command("k","o","RESERVE",List.of(item),null).normalized());
        assertThrows(IllegalArgumentException.class,()->new Command("k","o","RELEASE",List.of(item),null).normalized());
        assertThrows(IllegalArgumentException.class,()->new Command("k","o","REFUND",List.of(),null).normalized());
        assertThrows(IllegalArgumentException.class,()->new Command("k","o","RESERVE",List.of(item,item),LocalDateTime.now()).normalized());
    }
    @Test void neverAllowsSameReadAndWriteCredential(){
        assertThrows(IllegalArgumentException.class,()->new InternalIdentityFilter("a".repeat(32),"a".repeat(32)));
        assertThrows(IllegalArgumentException.class,()->new InternalIdentityFilter("short","b".repeat(32)));
    }
}

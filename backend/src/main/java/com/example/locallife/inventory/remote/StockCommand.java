package com.example.locallife.inventory.remote;

import java.time.LocalDateTime;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;

/** Versioned wire contract; inventory service owns the execution and receipt. */
public record StockCommand(String commandId,String orderId,String kind,List<Item> items,LocalDateTime expiresAt) {
    public record Item(String itemType,Long itemId,int quantity) { }
    public String hash(){
        String text="inventory.v1\n"+orderId+"\n"+kind+"\n"+expiresAt;
        for(var item:items.stream().sorted(Comparator.comparing(Item::itemType).thenComparing(Item::itemId)).toList())
            text+="\n"+item.itemType+":"+item.itemId+":"+item.quantity;
        try{return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8)));}
        catch(Exception error){throw new IllegalStateException(error);}
    }
}

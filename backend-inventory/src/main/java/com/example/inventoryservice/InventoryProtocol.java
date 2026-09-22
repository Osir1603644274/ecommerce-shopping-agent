package com.example.inventoryservice;

import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import java.time.LocalDateTime;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;

public final class InventoryProtocol {
    private InventoryProtocol() { }
    public record Item(@Pattern(regexp="PRODUCT|LOCAL_DEAL") @NotNull String itemType,
                       @NotNull @Positive Long itemId, @Min(1) @Max(100000) int quantity) { }
    public record Command(@NotBlank @Size(max=128) String commandId,
                          @NotBlank @Size(max=36) String orderId,
                          @NotNull @Pattern(regexp="RESERVE|CONFIRM|RELEASE|REFUND|RESTORE_ALL|RETURN_SELLABLE|RETURN_QUARANTINE") String kind,
                          @NotNull @Size(max=50) List<@Valid Item> items,
                          LocalDateTime expiresAt) {
        public Command normalized() {
            var sorted=new ArrayList<>(items);
            sorted.sort(Comparator.comparing(Item::itemType).thenComparing(Item::itemId));
            if (Set.of("RESERVE","REFUND","RETURN_SELLABLE","RETURN_QUARANTINE").contains(kind) == sorted.isEmpty())
                throw new IllegalArgumentException("invalid_command_items");
            if(kind.startsWith("RETURN_") && sorted.size()!=1) throw new IllegalArgumentException("return_requires_one_item");
            if ("RESERVE".equals(kind) && expiresAt==null) throw new IllegalArgumentException("expires_at_required");
            if (!"RESERVE".equals(kind) && expiresAt!=null) throw new IllegalArgumentException("unexpected_expires_at");
            for(int i=1;i<sorted.size();i++)
                if(sorted.get(i).itemType().equals(sorted.get(i-1).itemType()) && sorted.get(i).itemId().equals(sorted.get(i-1).itemId()))
                    throw new IllegalArgumentException("duplicate_item");
            return new Command(commandId,orderId,kind,List.copyOf(sorted),expiresAt);
        }
        public String hash() {
            var normalized=normalized();
            String text="inventory.v1\n"+normalized.orderId+"\n"+normalized.kind+"\n"+normalized.expiresAt;
            for(var item:normalized.items)text+="\n"+item.itemType+":"+item.itemId+":"+item.quantity;
            try{return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8)));}
            catch(Exception error){throw new IllegalStateException(error);}
        }
    }
    public record Stock(Long id,String itemType,Long itemId,int totalQuantity,int availableQuantity,
                        int reservedQuantity,int soldQuantity,long version) { }
    public record StockQuery(@NotNull @Pattern(regexp="PRODUCT|LOCAL_DEAL") String itemType,
                             @NotEmpty @Size(max=500) List<@NotNull @Positive Long> itemIds) { }
    public record Reservation(String id,String orderId,Long stockId,int quantity,int refundedQuantity,
                              String status,LocalDateTime expiresAt) { }
    public record Receipt(String commandId,String orderId,String requestHash,String status,String reason,
                          List<Reservation> reservations) { }
}

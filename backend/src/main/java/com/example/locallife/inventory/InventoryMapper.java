package com.example.locallife.inventory;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.LocalDateTime;

@Mapper
interface InventoryMapper {

    @Select("""
            SELECT id, item_type, item_id, total_quantity, available_quantity,
                   reserved_quantity, sold_quantity, version
            FROM inventory_stock
            WHERE item_type = #{itemType} AND item_id = #{itemId}
            """)
    InventoryStock findStock(
            @Param("itemType") String itemType,
            @Param("itemId") Long itemId
    );

    @Insert("""
            INSERT INTO inventory_stock(
                item_type, item_id, total_quantity, available_quantity,
                reserved_quantity, sold_quantity, version
            ) VALUES(#{itemType}, #{itemId}, #{quantity}, #{quantity}, 0, 0, 0)
            """)
    int insertStock(
            @Param("itemType") String itemType,
            @Param("itemId") Long itemId,
            @Param("quantity") int quantity
    );

    @Update("""
            UPDATE inventory_stock
            SET available_quantity = available_quantity - #{quantity},
                reserved_quantity = reserved_quantity + #{quantity},
                version = version + 1
            WHERE id = #{stockId} AND available_quantity >= #{quantity}
            """)
    int reserve(@Param("stockId") Long stockId, @Param("quantity") int quantity);

    @Update("""
            UPDATE inventory_stock
            SET reserved_quantity = reserved_quantity - #{quantity},
                sold_quantity = sold_quantity + #{quantity},
                version = version + 1
            WHERE id = #{stockId} AND reserved_quantity >= #{quantity}
            """)
    int confirm(@Param("stockId") Long stockId, @Param("quantity") int quantity);

    @Update("""
            UPDATE inventory_stock
            SET reserved_quantity = reserved_quantity - #{quantity},
                available_quantity = available_quantity + #{quantity},
                version = version + 1
            WHERE id = #{stockId} AND reserved_quantity >= #{quantity}
            """)
    int release(@Param("stockId") Long stockId, @Param("quantity") int quantity);

    @Insert("""
            INSERT INTO inventory_reservation(
                id, order_id, stock_id, quantity, status, expires_at
            ) VALUES(#{id}, #{orderId}, #{stockId}, #{quantity}, 'RESERVED', #{expiresAt})
            """)
    int insertReservation(
            @Param("id") String id,
            @Param("orderId") String orderId,
            @Param("stockId") Long stockId,
            @Param("quantity") int quantity,
            @Param("expiresAt") LocalDateTime expiresAt
    );

    @Select("""
            SELECT id, order_id, stock_id, quantity, status, expires_at
            FROM inventory_reservation WHERE order_id = #{orderId}
            """)
    InventoryReservation findReservationByOrder(@Param("orderId") String orderId);

    @Update("""
            UPDATE inventory_reservation
            SET status = #{targetStatus}, updated_at = CURRENT_TIMESTAMP
            WHERE order_id = #{orderId} AND status = 'RESERVED'
            """)
    int transitionReservation(
            @Param("orderId") String orderId,
            @Param("targetStatus") String targetStatus
    );
}

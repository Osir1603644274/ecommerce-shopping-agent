package com.example.locallife.search;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

@Mapper
interface ProductSearchProjectionCursorMapper {
    @Select("SELECT entity_version FROM product_search_projection_cursor WHERE product_id = #{productId} FOR UPDATE")
    Long findVersionForUpdate(@Param("productId") long productId);

    @Insert("""
            INSERT INTO product_search_projection_cursor(product_id, entity_version, event_id, operation)
            VALUES(#{productId}, #{entityVersion}, #{eventId}, #{operation})
            ON DUPLICATE KEY UPDATE
                event_id = IF(VALUES(entity_version) > entity_version, VALUES(event_id), event_id),
                operation = IF(VALUES(entity_version) > entity_version, VALUES(operation), operation),
                indexed_at = IF(VALUES(entity_version) > entity_version, CURRENT_TIMESTAMP, indexed_at),
                entity_version = GREATEST(entity_version, VALUES(entity_version))
            """)
    int advance(
            @Param("productId") long productId,
            @Param("entityVersion") long entityVersion,
            @Param("eventId") String eventId,
            @Param("operation") String operation
    );
}

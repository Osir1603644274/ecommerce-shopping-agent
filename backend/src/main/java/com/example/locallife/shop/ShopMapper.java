package com.example.locallife.shop;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.SelectProvider;
import org.apache.ibatis.annotations.Update;

import java.util.List;

@Mapper
interface ShopMapper {

    String COLUMNS = """
            id, name, type_id, address, avg_price, phone, longitude, latitude,
            coordinate_system, district, anchor_place_id, anchor_place_name,
            data_nature, source, source_entity_id, original_name, original_address,
            original_longitude, original_latitude, localization_version,
            created_at, updated_at
            """;

    @Select("SELECT " + COLUMNS + " FROM shop ORDER BY id")
    List<Shop> findAll();

    @Select("SELECT " + COLUMNS + " FROM shop WHERE type_id = #{typeId} ORDER BY id")
    List<Shop> findByTypeId(@Param("typeId") Long typeId);

    @SelectProvider(type = ShopSqlProvider.class, method = "findByFilters")
    List<Shop> findByFilters(@Param("typeId") Long typeId, @Param("name") String name);

    @Select("SELECT " + COLUMNS + " FROM shop WHERE id = #{id}")
    Shop findById(@Param("id") Long id);

    @Update("""
            UPDATE shop
            SET address = #{request.address},
                avg_price = #{request.avgPrice},
                phone = #{request.phone},
                updated_at = CURRENT_TIMESTAMP
            WHERE id = #{id}
            """)
    int updateDetails(@Param("id") Long id, @Param("request") UpdateShopRequest request);
}

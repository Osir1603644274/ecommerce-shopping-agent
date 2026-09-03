package com.example.locallife.shop;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
interface ShopTypeMapper {

    @Select("SELECT id, name, sort, created_at, updated_at FROM shop_type ORDER BY sort")
    List<ShopType> findAll();

    @Select("SELECT id, name, sort, created_at, updated_at FROM shop_type WHERE id = #{id}")
    ShopType findById(@Param("id") Long id);
}

package com.example.locallife.product;

import org.apache.ibatis.annotations.*;
import java.util.List;

@Mapper
public interface ProductFavoriteMapper {
    @Select("SELECT p.* FROM product_favorite f JOIN product p ON p.id = f.product_id "
            + "WHERE f.user_id = #{owner} ORDER BY f.created_at DESC, f.product_id DESC LIMIT 200")
    List<Product> list(String owner);

    @Insert("INSERT INTO product_favorite(user_id, product_id) VALUES(#{owner}, #{productId}) "
            + "ON DUPLICATE KEY UPDATE product_id = VALUES(product_id)")
    int add(@Param("owner") String owner, @Param("productId") long productId);

    @Delete("DELETE FROM product_favorite WHERE user_id = #{owner} AND product_id = #{productId}")
    int remove(@Param("owner") String owner, @Param("productId") long productId);
}

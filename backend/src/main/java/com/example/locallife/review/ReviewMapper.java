package com.example.locallife.review;

import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.util.List;

@Mapper
interface ReviewMapper {

    String COLUMNS = """
            id, shop_id, owner_user_id, content, tags, source, language, content_zh, translation_status,
            source_review_id, source_user_id, stars, source_shop_name, evidence_scope,
            created_at, updated_at
            """;

    @Select("SELECT " + COLUMNS + " FROM review ORDER BY id")
    List<Review> findAll();

    @Select("SELECT " + COLUMNS + " FROM review WHERE shop_id = #{shopId} ORDER BY id")
    List<Review> findByShopId(@Param("shopId") Long shopId);

    @Insert("""
            INSERT INTO review (
                id, shop_id, owner_user_id, content, tags, source, language, content_zh,
                translation_status, source_review_id, source_user_id, stars,
                source_shop_name, evidence_scope
            )
            VALUES (
                #{reviewId}, #{shopId}, #{ownerUserId}, #{content}, #{tags}, 'user', 'zh', NULL,
                'not_required', NULL, NULL, NULL, NULL, NULL
            )
            """)
    int insert(
            @Param("reviewId") String reviewId,
            @Param("shopId") Long shopId,
            @Param("ownerUserId") String ownerUserId,
            @Param("content") String content,
            @Param("tags") String tags
    );

    @Select("SELECT " + COLUMNS + " FROM review WHERE id = #{reviewId}")
    Review findById(@Param("reviewId") String reviewId);

    @Update("""
            UPDATE review
            SET content = #{content}, tags = #{tags}, updated_at = CURRENT_TIMESTAMP
            WHERE id = #{reviewId}
            """)
    int update(
            @Param("reviewId") String reviewId,
            @Param("content") String content,
            @Param("tags") String tags
    );

    @Delete("DELETE FROM review WHERE id = #{reviewId}")
    int deleteById(@Param("reviewId") String reviewId);
}

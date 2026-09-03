package com.example.locallife.behavior;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
interface UserBehaviorMapper {

    String COLUMNS = """
            id, user_id, shop_id, behavior_type, score, source,
            source_user_id, source_review_id, occurred_at, created_at
            """;

    @Insert("""
            INSERT INTO user_behavior (
                id, user_id, shop_id, behavior_type, score, source,
                source_user_id, source_review_id, occurred_at
            )
            VALUES (
                #{id}, #{userId}, #{shopId}, #{behaviorType}, #{score}, #{source},
                #{sourceUserId}, #{sourceReviewId}, #{occurredAt}
            )
            """)
    int insert(UserBehavior behavior);

    @Select("SELECT " + COLUMNS + " FROM user_behavior WHERE id = #{id}")
    UserBehavior findById(@Param("id") String id);

    @Select("SELECT " + COLUMNS + """
             FROM user_behavior
             WHERE user_id = #{userId}
             ORDER BY occurred_at DESC, created_at DESC
             LIMIT #{limit}
            """)
    List<UserBehavior> findByUserId(@Param("userId") String userId, @Param("limit") int limit);

    @Select("SELECT " + COLUMNS + """
             FROM user_behavior
             ORDER BY occurred_at DESC, created_at DESC
             LIMIT #{limit}
            """)
    List<UserBehavior> findRecent(@Param("limit") int limit);
}

package com.example.locallife.identity;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
interface UserAccountMapper {

    @Insert("""
            INSERT INTO user_account (id, username, password_hash, enabled, token_version)
            VALUES (#{id}, #{username}, #{passwordHash}, #{enabled}, #{tokenVersion})
            """)
    int insert(UserAccount account);

    @Insert("INSERT INTO user_role (user_id, role_name) VALUES (#{userId}, #{role})")
    int insertRole(@Param("userId") String userId, @Param("role") String role);

    @Select("""
            SELECT id, username, password_hash, enabled, token_version, created_at, updated_at
            FROM user_account
            WHERE username = #{username}
            """)
    UserAccount findByUsername(@Param("username") String username);

    @Select("""
            SELECT id, username, password_hash, enabled, token_version, created_at, updated_at
            FROM user_account
            WHERE id = #{id}
            """)
    UserAccount findById(@Param("id") String id);

    @Select("SELECT role_name FROM user_role WHERE user_id = #{userId} ORDER BY role_name")
    List<String> findRoles(@Param("userId") String userId);

    @Insert("""
            INSERT INTO auth_session_audit (family_id, user_id, event_type, token_jti)
            VALUES (#{familyId}, #{userId}, #{eventType}, #{tokenJti})
            """)
    int insertAudit(
            @Param("familyId") String familyId,
            @Param("userId") String userId,
            @Param("eventType") String eventType,
            @Param("tokenJti") String tokenJti
    );
}

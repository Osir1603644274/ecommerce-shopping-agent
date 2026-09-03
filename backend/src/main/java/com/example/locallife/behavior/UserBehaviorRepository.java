package com.example.locallife.behavior;

import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

@Repository
public class UserBehaviorRepository {

    private final UserBehaviorMapper mapper;

    public UserBehaviorRepository(UserBehaviorMapper mapper) {
        this.mapper = mapper;
    }

    public void insert(UserBehavior behavior) {
        mapper.insert(behavior);
    }

    public Optional<UserBehavior> findById(String id) {
        return Optional.ofNullable(mapper.findById(id));
    }

    public List<UserBehavior> findByUserId(String userId, int limit) {
        return mapper.findByUserId(userId, limit);
    }

    public List<UserBehavior> findRecent(int limit) {
        return mapper.findRecent(limit);
    }
}

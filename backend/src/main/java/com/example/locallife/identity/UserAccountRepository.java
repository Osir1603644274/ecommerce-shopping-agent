package com.example.locallife.identity;

import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

@Repository
class UserAccountRepository {

    private final UserAccountMapper mapper;

    UserAccountRepository(UserAccountMapper mapper) {
        this.mapper = mapper;
    }

    void insert(UserAccount account, String initialRole) {
        mapper.insert(account);
        mapper.insertRole(account.id(), initialRole);
    }

    Optional<UserAccount> findByUsername(String username) {
        return Optional.ofNullable(mapper.findByUsername(username));
    }

    Optional<UserAccount> findById(String id) {
        return Optional.ofNullable(mapper.findById(id));
    }

    List<String> findRoles(String userId) {
        return mapper.findRoles(userId);
    }

    void audit(String familyId, String userId, String eventType, String tokenJti) {
        mapper.insertAudit(familyId, userId, eventType, tokenJti);
    }
}

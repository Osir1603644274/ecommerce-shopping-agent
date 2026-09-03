package com.example.locallife.identity;

import org.springframework.dao.DuplicateKeyException;
import org.springframework.http.HttpStatus;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.oauth2.jwt.JwtException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Locale;
import java.util.UUID;

@Service
class AuthService {

    private static final String DEFAULT_ROLE = "USER";

    private final UserAccountRepository accounts;
    private final PasswordEncoder passwordEncoder;
    private final JwtService jwtService;
    private final RefreshTokenStore refreshTokens;

    AuthService(
            UserAccountRepository accounts,
            PasswordEncoder passwordEncoder,
            JwtService jwtService,
            RefreshTokenStore refreshTokens
    ) {
        this.accounts = accounts;
        this.passwordEncoder = passwordEncoder;
        this.jwtService = jwtService;
        this.refreshTokens = refreshTokens;
    }

    @Transactional
    AuthTokensResponse register(RegisterRequest request) {
        String username = normalizeUsername(request.username());
        UserAccount account = new UserAccount(
                UUID.randomUUID().toString(),
                username,
                passwordEncoder.encode(request.password()),
                true,
                0,
                null,
                null
        );
        try {
            accounts.insert(account, DEFAULT_ROLE);
        } catch (DuplicateKeyException exception) {
            throw new AuthFailureException(HttpStatus.CONFLICT, "用户名已存在");
        }
        return issue(account, List.of(DEFAULT_ROLE), null, "REGISTERED");
    }

    AuthTokensResponse login(LoginRequest request) {
        String username = normalizeUsername(request.username());
        UserAccount account = accounts.findByUsername(username)
                .filter(UserAccount::enabled)
                .orElseThrow(this::invalidCredentials);
        if (!passwordEncoder.matches(request.password(), account.passwordHash())) {
            throw invalidCredentials();
        }
        return issue(account, accounts.findRoles(account.id()), null, "LOGIN");
    }

    AuthTokensResponse refresh(RefreshRequest request) {
        JwtService.RefreshClaims claims = decodeRefresh(request.refreshToken());
        String tokenHash = jwtService.hash(request.refreshToken());
        if (!refreshTokens.consume(claims.jti(), claims.familyId(), tokenHash)) {
            refreshTokens.revokeFamily(claims.familyId());
            accounts.audit(claims.familyId(), claims.userId(), "REUSE_DETECTED", claims.jti());
            throw new AuthFailureException(HttpStatus.UNAUTHORIZED, "刷新令牌已失效或被重复使用");
        }

        UserAccount account = accounts.findById(claims.userId())
                .filter(UserAccount::enabled)
                .orElseThrow(() -> new AuthFailureException(HttpStatus.UNAUTHORIZED, "用户不可用"));
        return issue(
                account,
                accounts.findRoles(account.id()),
                claims.familyId(),
                "ROTATED"
        );
    }

    void logout(RefreshRequest request) {
        JwtService.RefreshClaims claims = decodeRefresh(request.refreshToken());
        refreshTokens.revokeFamily(claims.familyId());
        accounts.audit(claims.familyId(), claims.userId(), "LOGOUT", claims.jti());
    }

    private AuthTokensResponse issue(
            UserAccount account,
            List<String> roles,
            String familyId,
            String auditEvent
    ) {
        JwtService.IssuedTokens issued = jwtService.issue(account, roles, familyId);
        refreshTokens.save(
                issued.refreshJti(),
                issued.familyId(),
                jwtService.hash(issued.refreshToken()),
                jwtService.refreshTtl()
        );
        accounts.audit(issued.familyId(), account.id(), auditEvent, issued.refreshJti());
        return new AuthTokensResponse(
                issued.accessToken(),
                issued.refreshToken(),
                "Bearer",
                jwtService.accessTtlSeconds(),
                jwtService.refreshTtlSeconds(),
                new AuthUserResponse(account.id(), account.username(), List.copyOf(roles))
        );
    }

    private JwtService.RefreshClaims decodeRefresh(String token) {
        try {
            return jwtService.decodeRefresh(token);
        } catch (JwtException | IllegalArgumentException exception) {
            throw new AuthFailureException(HttpStatus.UNAUTHORIZED, "刷新令牌无效或已过期");
        }
    }

    private AuthFailureException invalidCredentials() {
        return new AuthFailureException(HttpStatus.UNAUTHORIZED, "用户名或密码错误");
    }

    private static String normalizeUsername(String username) {
        return username.trim().toLowerCase(Locale.ROOT);
    }
}

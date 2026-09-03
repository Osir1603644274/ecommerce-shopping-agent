package com.example.locallife.behavior;

import com.example.locallife.common.ApiResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@Validated
@RestController
@RequestMapping("/api/user-behaviors")
public class UserBehaviorController {

    private final UserBehaviorService userBehaviorService;

    public UserBehaviorController(UserBehaviorService userBehaviorService) {
        this.userBehaviorService = userBehaviorService;
    }

    @PostMapping
    public ResponseEntity<ApiResponse<UserBehaviorResponse>> createBehavior(
            @Valid @RequestBody CreateUserBehaviorRequest request,
            Authentication authentication
    ) {
        String effectiveUserId = authentication.getAuthorities().stream()
                .anyMatch(authority -> "ROLE_SERVICE".equals(authority.getAuthority()))
                ? request.userId().trim()
                : authentication.getName();
        UserBehaviorResponse response = userBehaviorService.createBehavior(request, effectiveUserId);
        return ResponseEntity.status(HttpStatus.CREATED).body(ApiResponse.ok(response));
    }

    @GetMapping
    public ApiResponse<List<UserBehaviorResponse>> listUserBehaviors(
            @RequestParam @NotBlank(message = "用户编号不能为空") String userId,
            @RequestParam(required = false) Integer limit
    ) {
        return ApiResponse.ok(userBehaviorService.listUserBehaviors(userId, limit));
    }
}

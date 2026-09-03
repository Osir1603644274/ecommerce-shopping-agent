package com.example.locallife.identity;

import com.example.locallife.common.ApiResponse;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/identity")
public class IdentityController {

    @GetMapping("/me")
    public ApiResponse<AuthenticatedIdentityResponse> me(Authentication authentication) {
        List<String> roles = authentication.getAuthorities().stream()
                .map(authority -> authority.getAuthority())
                .filter(value -> value.startsWith("ROLE_"))
                .map(value -> value.substring("ROLE_".length()))
                .sorted()
                .toList();
        return ApiResponse.ok(new AuthenticatedIdentityResponse(authentication.getName(), roles));
    }
}

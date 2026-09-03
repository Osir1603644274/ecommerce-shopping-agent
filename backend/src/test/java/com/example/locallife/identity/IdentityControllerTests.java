package com.example.locallife.identity;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.security.test.context.support.WithMockUser;
import org.springframework.test.web.servlet.MockMvc;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
class IdentityControllerTests {
    @Autowired
    private MockMvc mockMvc;

    @Test
    void anonymousCallerCannotIntrospectIdentity() throws Exception {
        mockMvc.perform(get("/api/identity/me"))
                .andExpect(status().isUnauthorized());
    }

    @Test
    @WithMockUser(username = "server-owner-1", roles = {"USER", "ADMIN"})
    void authenticatedIdentityComesFromSpringSecurity() throws Exception {
        mockMvc.perform(get("/api/identity/me"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.userId").value("server-owner-1"))
                .andExpect(jsonPath("$.data.roles[0]").value("ADMIN"))
                .andExpect(jsonPath("$.data.roles[1]").value("USER"));
    }
}

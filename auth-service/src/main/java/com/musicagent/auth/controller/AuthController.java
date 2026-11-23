package com.musicagent.auth.controller;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Map;

import com.musicagent.auth.dto.Credentials;
import com.musicagent.auth.dto.UserResponse;
import com.musicagent.auth.service.AuthService;
import com.musicagent.auth.service.AuthService.AuthenticatedSession;
import com.musicagent.auth.service.SessionService;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.CookieValue;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/auth")
public class AuthController {
    private static final String COOKIE = "MUSIC_SESSION";

    private final AuthService authService;
    private final SessionService sessionService;

    @Value("${music.session.secure:true}")
    private boolean secureCookie;

    public AuthController(AuthService authService, SessionService sessionService) {
        this.authService = authService;
        this.sessionService = sessionService;
    }

    @PostMapping("/register")
    public UserResponse register(@RequestBody Credentials input, HttpServletResponse response) {
        AuthenticatedSession session = authService.register(input);
        setSessionCookie(response, session);
        return UserResponse.from(session.user());
    }

    @PostMapping("/login")
    public UserResponse login(@RequestBody Credentials input, HttpServletResponse response) {
        AuthenticatedSession session = authService.login(input);
        setSessionCookie(response, session);
        return UserResponse.from(session.user());
    }

    @GetMapping("/me")
    public Map<String, String> me(@CookieValue(name = COOKIE, required = false) String token) {
        return token == null ? Map.of() : sessionService.findUser(token)
            .map(user -> Map.of("id", user.id(), "username", user.username()))
            .orElseGet(Map::of);
    }

    @PostMapping("/logout")
    public Map<String, Boolean> logout(@CookieValue(name = COOKIE, required = false) String token, HttpServletResponse response) {
        if (token != null) sessionService.revoke(token);
        response.addHeader("Set-Cookie", COOKIE + "=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" + (secureCookie ? "; Secure" : ""));
        return Map.of("ok", true);
    }

    private void setSessionCookie(HttpServletResponse response, AuthenticatedSession session) {
        int maxAge = (int) Math.max(0, ChronoUnit.SECONDS.between(Instant.now(), session.expiresAt()));
        response.addHeader("Set-Cookie", COOKIE + "=" + session.token() + "; Path=/; Max-Age=" + maxAge + "; HttpOnly; SameSite=Lax" + (secureCookie ? "; Secure" : ""));
    }
}

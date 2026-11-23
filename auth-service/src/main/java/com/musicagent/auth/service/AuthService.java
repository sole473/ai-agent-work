package com.musicagent.auth.service;

import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.UUID;

import com.musicagent.auth.dto.Credentials;
import com.musicagent.auth.repository.PlatformUser;
import com.musicagent.auth.repository.UserRepository;
import com.musicagent.auth.service.SessionService.IssuedSession;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.http.HttpStatus;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

@Service
public class AuthService {
    private final UserRepository users;
    private final SessionService sessions;
    private final BCryptPasswordEncoder passwords = new BCryptPasswordEncoder(12);

    public AuthService(UserRepository users, SessionService sessions) {
        this.users = users;
        this.sessions = sessions;
    }

    @Transactional
    public AuthenticatedSession register(Credentials input) {
        String username = normalizeUsername(input == null ? null : input.username());
        String password = input == null ? null : input.password();
        validateCredentials(username, password);

        String userId = UUID.randomUUID().toString().replace("-", "");
        try {
            users.insert(userId, username, passwords.encode(password));
        } catch (DuplicateKeyException ex) {
            throw new ResponseStatusException(HttpStatus.CONFLICT, "该用户名已注册。");
        }

        PlatformUser user = new PlatformUser(userId, username, null);
        return authenticatedSession(user);
    }

    public AuthenticatedSession login(Credentials input) {
        String username = normalizeUsername(input == null ? null : input.username());
        String password = input == null ? null : input.password();
        PlatformUser user = users.findByUsername(username).orElse(null);
        if (user == null || password == null || password.getBytes(StandardCharsets.UTF_8).length > 72 || !passwords.matches(password, user.passwordHash())) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "用户名或密码错误。");
        }
        return authenticatedSession(user);
    }

    private AuthenticatedSession authenticatedSession(PlatformUser user) {
        IssuedSession issued = sessions.issue(user.id());
        return new AuthenticatedSession(user, issued.token(), issued.expiresAt());
    }

    private String normalizeUsername(String username) {
        return username == null ? "" : username.trim().toLowerCase(Locale.ROOT);
    }

    private void validateCredentials(String username, String password) {
        if (!username.matches("[a-z0-9._@+-]{3,80}") || password == null || password.length() < 10 || password.getBytes(StandardCharsets.UTF_8).length > 72) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "用户名格式无效，密码长度需为 10 到 72 个字符。");
        }
    }

    public record AuthenticatedSession(PlatformUser user, String token, java.time.Instant expiresAt) {}
}

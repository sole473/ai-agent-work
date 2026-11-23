package com.musicagent.auth.service;

import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.UUID;

import com.musicagent.auth.dto.Credentials;
import com.musicagent.auth.repository.PlatformUser;
import com.musicagent.auth.repository.UserRepository;
import com.musicagent.auth.service.SessionService.IssuedSession;
import org.springframework.http.HttpStatus;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.dao.DuplicateKeyException;
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

    public AuthenticatedSession login(Credentials input) {
        String username = normalizeUsername(input == null ? null : input.username());
        String password = input == null ? null : input.password();
        PlatformUser user = users.findByUsername(username).orElse(null);
        if (user == null || password == null || password.getBytes(StandardCharsets.UTF_8).length > 72 || !passwords.matches(password, user.passwordHash())) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "用户名或密码错误。");
        }
        return authenticatedSession(user);
    }

    public PlatformUser register(Credentials input) {
        String username = normalizeUsername(input == null ? null : input.username());
        String password = input == null ? null : input.password();
        if (!username.matches("[a-z0-9._-]{3,80}")) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "用户名须为 3 至 80 位字母、数字、点、下划线或连字符。");
        }
        int passwordBytes = password == null ? 0 : password.getBytes(StandardCharsets.UTF_8).length;
        if (password == null || passwordBytes < 8 || passwordBytes > 72) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "密码按 UTF-8 编码须为 8 至 72 字节。");
        }
        if (users.existsByUsername(username)) {
            throw new ResponseStatusException(HttpStatus.CONFLICT, "用户名已存在。");
        }
        PlatformUser user = new PlatformUser(UUID.randomUUID().toString().replace("-", ""), username, passwords.encode(password));
        try {
            users.insert(user.id(), user.username(), user.passwordHash());
        } catch (DuplicateKeyException exception) {
            throw new ResponseStatusException(HttpStatus.CONFLICT, "用户名已存在。", exception);
        }
        return user;
    }

    private AuthenticatedSession authenticatedSession(PlatformUser user) {
        IssuedSession issued = sessions.issue(user.id());
        return new AuthenticatedSession(user, issued.token(), issued.expiresAt());
    }

    private String normalizeUsername(String username) {
        return username == null ? "" : username.trim().toLowerCase(Locale.ROOT);
    }

    public record AuthenticatedSession(PlatformUser user, String token, java.time.Instant expiresAt) {}
}

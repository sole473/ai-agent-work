package com.musicagent.auth.service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.HexFormat;
import java.util.Optional;

import com.musicagent.auth.repository.PlatformUser;
import com.musicagent.auth.repository.SessionRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

@Service
public class SessionService {
    private final SessionRepository sessions;
    private final SecureRandom random = new SecureRandom();

    @Value("${music.session.ttl-hours:168}")
    private long ttlHours;

    public SessionService(SessionRepository sessions) {
        this.sessions = sessions;
    }

    public IssuedSession issue(String userId) {
        byte[] bytes = new byte[32];
        random.nextBytes(bytes);
        String rawToken = HexFormat.of().formatHex(bytes);
        Instant expiresAt = Instant.now().plus(ttlHours, ChronoUnit.HOURS);
        sessions.insert(sha256(rawToken), userId, expiresAt);
        return new IssuedSession(rawToken, expiresAt);
    }

    public Optional<PlatformUser> findUser(String rawToken) {
        return sessions.findUserByTokenHash(sha256(rawToken));
    }

    public void revoke(String rawToken) {
        sessions.deleteByTokenHash(sha256(rawToken));
    }

    private String sha256(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception ex) {
            throw new IllegalStateException("无法计算会话令牌摘要。", ex);
        }
    }

    public record IssuedSession(String token, Instant expiresAt) {}
}

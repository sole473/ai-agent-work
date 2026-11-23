package com.musicagent.auth.repository;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.Optional;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class SessionRepository {
    private final JdbcTemplate db;

    public SessionRepository(JdbcTemplate db) {
        this.db = db;
    }

    public void insert(String tokenHash, String userId, Instant expiresAt) {
        db.update("INSERT INTO music_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
            tokenHash, userId, Timestamp.from(expiresAt));
    }

    public Optional<PlatformUser> findUserByTokenHash(String tokenHash) {
        List<PlatformUser> users = db.query(
            "SELECT u.id,u.username FROM music_sessions s JOIN music_users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>CURRENT_TIMESTAMP",
            (rs, row) -> new PlatformUser(rs.getString("id"), rs.getString("username"), null),
            tokenHash
        );
        return users.stream().findFirst();
    }

    public void deleteByTokenHash(String tokenHash) {
        db.update("DELETE FROM music_sessions WHERE token_hash=?", tokenHash);
    }
}

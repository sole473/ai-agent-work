package com.musicagent.auth.repository;

import java.util.List;
import java.util.Optional;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class UserRepository {
    private final JdbcTemplate db;

    public UserRepository(JdbcTemplate db) {
        this.db = db;
    }

    public Optional<PlatformUser> findByUsername(String username) {
        List<PlatformUser> users = db.query(
            "SELECT id,username,password_hash FROM music_users WHERE username=?",
            (rs, row) -> new PlatformUser(rs.getString("id"), rs.getString("username"), rs.getString("password_hash")),
            username
        );
        return users.stream().findFirst();
    }

    public boolean existsByUsername(String username) {
        Integer count = db.queryForObject("SELECT COUNT(*) FROM music_users WHERE username=?", Integer.class, username);
        return count != null && count > 0;
    }

    public void insert(String id, String username, String passwordHash) {
        db.update("INSERT INTO music_users(id,username,password_hash) VALUES(?,?,?)", id, username, passwordHash);
    }
}

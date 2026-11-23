CREATE TABLE IF NOT EXISTS music_users (
  id CHAR(32) PRIMARY KEY,
  username VARCHAR(80) NOT NULL UNIQUE,
  password_hash VARCHAR(100) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS music_sessions (
  token_hash CHAR(64) PRIMARY KEY,
  user_id CHAR(32) NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_music_sessions_user (user_id),
  CONSTRAINT fk_music_sessions_user FOREIGN KEY (user_id) REFERENCES music_users(id) ON DELETE CASCADE
);

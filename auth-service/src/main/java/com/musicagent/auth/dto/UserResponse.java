package com.musicagent.auth.dto;

import com.musicagent.auth.repository.PlatformUser;

public record UserResponse(String id, String username) {
    public static UserResponse from(PlatformUser user) {
        return new UserResponse(user.id(), user.username());
    }
}

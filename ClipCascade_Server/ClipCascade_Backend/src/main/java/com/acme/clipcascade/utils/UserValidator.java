package com.acme.clipcascade.utils;

import com.acme.clipcascade.constants.RoleConstants;
import com.acme.clipcascade.model.Users;

public class UserValidator {
    /**
     * Server-side password policy. Clients normally submit the SHA3-512 hex of
     * the raw password (128 chars), which always satisfies this window. The
     * bounds still reject trivial/empty values and oversized inputs.
     */
    private static final int PASSWORD_MIN_LENGTH = 8;
    private static final int PASSWORD_MAX_LENGTH = 128;

    public static boolean isValid(Users user) {
        return user != null
                && user.getUsername() != null && !user.getUsername().isBlank()
                && !user.getUsername().startsWith(" ") && !user.getUsername().endsWith(" ")
                && isValidPassword(user.getPassword())
                && user.getRole() != null && (user.getRole().equals(RoleConstants.ADMIN)
                        || user.getRole().equals(RoleConstants.USER));
    }

    public static boolean isValidUsername(String username) {
        return username != null && !username.isBlank()
                && !username.startsWith(" ") && !username.endsWith(" ");
    }

    public static boolean isValidPassword(String password) {
        return password != null
                && password.length() >= PASSWORD_MIN_LENGTH
                && password.length() <= PASSWORD_MAX_LENGTH;
    }

    public static boolean isValidRole(String role) {
        return role != null && (role.equals(RoleConstants.ADMIN) || role.equals(RoleConstants.USER));
    }
}

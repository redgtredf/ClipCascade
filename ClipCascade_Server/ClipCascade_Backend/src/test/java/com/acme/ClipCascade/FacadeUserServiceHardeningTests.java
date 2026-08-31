package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import com.acme.clipcascade.config.ClipCascadeProperties;
import com.acme.clipcascade.constants.RoleConstants;
import com.acme.clipcascade.model.Users;
import com.acme.clipcascade.service.FacadeUserService;
import com.acme.clipcascade.service.UserInfoService;
import com.acme.clipcascade.service.UserService;
import com.acme.clipcascade.utils.IpAddressResolver;

/**
 * Signup hardening (username enumeration via timing) and self-service
 * password-change verification. Mock-based: dummy values only.
 */
class FacadeUserServiceHardeningTests {

    private static final String VALID_PASSWORD = "dummy-password-123";

    private final UserService userService = mock(UserService.class);
    private final UserInfoService userInfoService = mock(UserInfoService.class);

    private FacadeUserService facade() {
        ClipCascadeProperties properties = new ClipCascadeProperties();
        ReflectionTestUtils.setField(properties, "maxUserAccounts", -1L);
        return new FacadeUserService(
                userService,
                userInfoService,
                properties,
                new IpAddressResolver(properties));
    }

    private Users validUser(String username) {
        return new Users(username, VALID_PASSWORD, RoleConstants.USER, true);
    }

    @Test
    void takenUsernameStillPerformsTheExpensiveHash() {
        when(userService.hashPasswordForStorage(VALID_PASSWORD)).thenReturn("hashed-dummy");
        when(userService.userExists("taken-user")).thenReturn(true);
        when(userInfoService.userExists("taken-user")).thenReturn(false);

        assertNull(facade().registerUser(validUser("taken-user")));

        // The bcrypt work happens even for a taken username, so response
        // timing cannot reveal account existence.
        verify(userService).hashPasswordForStorage(VALID_PASSWORD);
        verify(userService, never()).createUser(any(Users.class));
        verify(userInfoService, never()).registerNewUser("taken-user");
    }

    @Test
    void freeUsernameIsStoredWithThePreComputedHash() {
        when(userService.hashPasswordForStorage(VALID_PASSWORD)).thenReturn("hashed-dummy");
        when(userService.userExists("free-user")).thenReturn(false);
        when(userInfoService.userExists("free-user")).thenReturn(false);
        when(userService.createUser(any(Users.class)))
                .thenAnswer(invocation -> invocation.getArgument(0));

        Users created = facade().registerUser(validUser("free-user"));

        assertNotNull(created);
        assertEquals("hashed-dummy", created.getPassword(),
                "the password must be hashed exactly once");
        verify(userInfoService).registerNewUser("free-user");
        verify(userService).createUser(any(Users.class));
    }

    @Test
    void passwordPolicyRejectsTooShortPasswordsBeforeHashing() {
        Users weakUser = new Users("weak-user", "short", RoleConstants.USER, true);

        assertNull(facade().registerUser(weakUser));

        verify(userService, never()).hashPasswordForStorage(anyString());
        verify(userService, never()).createUser(any(Users.class));
    }

    @Test
    void updatePasswordWithVerificationRejectsWrongOldPassword() {
        when(userService.verifyPassword("some-user", "wrong-old")).thenReturn(false);

        assertNull(facade().updatePasswordWithVerification(
                "some-user", "wrong-old", "new-password-123"));

        verify(userService, never()).updatePassword(anyString(), anyString());
    }

    @Test
    void updatePasswordWithVerificationRejectsMissingOldPassword() {
        when(userService.verifyPassword(eq("some-user"), eq(null))).thenReturn(false);

        assertNull(facade().updatePasswordWithVerification(
                "some-user", null, "new-password-123"));

        verify(userService, never()).updatePassword(anyString(), anyString());
    }

    @Test
    void updatePasswordWithVerificationRejectsPolicyViolatingNewPassword() {
        when(userService.verifyPassword("some-user", "correct-old")).thenReturn(true);

        assertNull(facade().updatePasswordWithVerification(
                "some-user", "correct-old", "short"));

        verify(userService, never()).updatePassword(anyString(), anyString());
    }

    @Test
    void updatePasswordWithVerificationAcceptsCorrectOldPassword() {
        when(userService.verifyPassword("some-user", "correct-old")).thenReturn(true);
        when(userService.updatePassword("some-user", "new-password-123"))
                .thenAnswer(invocation -> validUser("some-user"));

        assertNotNull(facade().updatePasswordWithVerification(
                "some-user", "correct-old", "new-password-123"));

        verify(userInfoService).setPasswordChangeTime(eq("some-user"), anyLong());
        verify(userService).updatePassword("some-user", "new-password-123");
    }

    @Test
    void adminResetStillWorksWithoutOldPassword() {
        when(userService.updatePassword("managed-user", "new-password-123"))
                .thenAnswer(invocation -> validUser("managed-user"));

        assertNotNull(facade().updatePassword("managed-user", "new-password-123"));

        verify(userService, never()).verifyPassword(anyString(), anyString());
        verify(userService).updatePassword("managed-user", "new-password-123");
    }
}

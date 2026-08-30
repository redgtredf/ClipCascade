package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.anyBoolean;
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
import com.acme.clipcascade.service.FacadeUserService;
import com.acme.clipcascade.service.UserInfoService;
import com.acme.clipcascade.service.UserService;

/**
 * Unit tests proving the initial admin account is bootstrapped from explicit
 * configuration, and ONLY when the user database is empty. Dummy values only.
 */
class FacadeUserServiceTests {

    private static final String DUMMY_ADMIN_USERNAME = "configured-admin";
    private static final String DUMMY_ADMIN_PASSWORD = "dummy-admin-password";

    private ClipCascadeProperties propertiesWithAdminCredentials() {
        ClipCascadeProperties properties = new ClipCascadeProperties();
        ReflectionTestUtils.setField(properties, "adminUsername", DUMMY_ADMIN_USERNAME);
        ReflectionTestUtils.setField(properties, "adminPassword", DUMMY_ADMIN_PASSWORD);
        return properties;
    }

    private FacadeUserService facadeWith(UserService userService, UserInfoService userInfoService) {
        return new FacadeUserService(userService, userInfoService, propertiesWithAdminCredentials());
    }

    @Test
    void adminIsBootstrappedFromConfiguredCredentialsOnlyWhenDatabaseIsEmpty() {
        UserService userService = mock(UserService.class);
        UserInfoService userInfoService = mock(UserInfoService.class);
        when(userService.isTableEmpty()).thenReturn(true);

        facadeWith(userService, userInfoService).insertDefaultAdminUserIfEmpty();

        verify(userService).doubleHashAndCreateUser(
                eq(DUMMY_ADMIN_USERNAME),
                eq(DUMMY_ADMIN_PASSWORD),
                eq(RoleConstants.ADMIN),
                eq(true));
        verify(userInfoService).registerNewUser(DUMMY_ADMIN_USERNAME);
    }

    @Test
    void existingDatabaseIsNeverTouchedOnRestart() {
        UserService userService = mock(UserService.class);
        UserInfoService userInfoService = mock(UserInfoService.class);
        when(userService.isTableEmpty()).thenReturn(false);

        facadeWith(userService, userInfoService).insertDefaultAdminUserIfEmpty();

        verify(userService, never()).doubleHashAndCreateUser(
                anyString(), anyString(), anyString(), anyBoolean());
        verify(userInfoService, never()).registerNewUser(anyString());
        verify(userService, never()).updatePassword(anyString(), anyString());
        verify(userService, never()).deleteUser(anyString());
    }

    @Test
    void bootstrapNeverUsesTheLegacyDefaultAdminAccount() {
        UserService userService = mock(UserService.class);
        UserInfoService userInfoService = mock(UserInfoService.class);
        when(userService.isTableEmpty()).thenReturn(true);

        facadeWith(userService, userInfoService).insertDefaultAdminUserIfEmpty();

        // No hardcoded 'admin' / 'admin123' bootstrap may ever happen again.
        verify(userService, never()).doubleHashAndCreateUser(
                eq("admin"), eq("admin123"), anyString(), anyBoolean());
        verify(userService, never()).doubleHashAndCreateUser(
                eq("admin"), anyString(), anyString(), anyBoolean());
        verify(userService, never()).doubleHashAndCreateUser(
                anyString(), eq("admin123"), anyString(), anyBoolean());

        assertEquals(DUMMY_ADMIN_USERNAME, propertiesWithAdminCredentials().getAdminUsername());
        assertEquals(DUMMY_ADMIN_PASSWORD, propertiesWithAdminCredentials().getAdminPassword());
    }
}

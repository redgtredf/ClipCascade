package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;

import com.acme.clipcascade.constants.RoleConstants;
import com.acme.clipcascade.model.Users;
import com.acme.clipcascade.repo.UserRepo;
import com.acme.clipcascade.service.FacadeUserService;
import com.acme.clipcascade.service.UserService;

/**
 * The startup in {@link com.acme.clipcascade.controller.ClipCascadeController}
 * bootstraps the admin account when the database is empty. These tests prove
 * the credentials come from configuration and that a "restart" (bootstrap
 * running again on a non-empty database) never resets a changed credential.
 * Dummy values only.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
@TestPropertySource(properties = {
        "spring.datasource.url=jdbc:h2:mem:admin-restart-test;MODE=PostgreSQL;DB_CLOSE_DELAY=-1"
})
class AdminBootstrapRestartTests {

    @Autowired
    private UserService userService;

    @Autowired
    private FacadeUserService facadeUserService;

    @Autowired
    private BCryptPasswordEncoder bCryptPasswordEncoder;

    @Autowired
    private UserRepo userRepo;

    @Value("${CC_ADMIN_USERNAME}")
    private String adminUsername;

    @Test
    void adminIsBootstrappedFromConfiguredCredentials() {
        Users admin = userRepo.findById(adminUsername).orElse(null);

        assertNotNull(admin, "admin user should be bootstrapped from CC_ADMIN_USERNAME");
        assertEquals(RoleConstants.ADMIN, admin.getRole());
        assertEquals(1, userRepo.findByRole(RoleConstants.ADMIN).size());
    }

    @Test
    void restartPreservesChangedAdminCredential() {
        assertNotNull(userRepo.findById(adminUsername).orElse(null));

        // simulate the admin changing their password
        String newPassword = "RestartedDummyPass456";
        assertNotNull(userService.updatePassword(adminUsername, newPassword));

        String storedHashAfterChange = userRepo.findById(adminUsername).get().getPassword();
        assertTrue(bCryptPasswordEncoder.matches(newPassword, storedHashAfterChange));

        // simulate a restart: bootstrap runs again, but the database is not empty
        facadeUserService.insertDefaultAdminUserIfEmpty();

        Users adminAfterRestart = userRepo.findById(adminUsername).orElse(null);
        assertNotNull(adminAfterRestart, "admin user must not be deleted or recreated on restart");
        assertEquals(storedHashAfterChange, adminAfterRestart.getPassword(),
                "admin credential must not be reset on restart");
        assertTrue(bCryptPasswordEncoder.matches(newPassword, adminAfterRestart.getPassword()));
        assertEquals(1, userRepo.findByRole(RoleConstants.ADMIN).size());
    }
}

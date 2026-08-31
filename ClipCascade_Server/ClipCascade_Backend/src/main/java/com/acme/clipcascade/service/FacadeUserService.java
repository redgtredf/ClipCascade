package com.acme.clipcascade.service;

import java.util.Set;
import java.util.stream.Collectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import com.acme.clipcascade.config.ClipCascadeProperties;
import com.acme.clipcascade.constants.RoleConstants;
import com.acme.clipcascade.model.IpAttemptDetails;
import com.acme.clipcascade.model.UserInfo;
import com.acme.clipcascade.model.Users;
import com.acme.clipcascade.utils.IpAddressResolver;
import com.acme.clipcascade.utils.TimeUtility;
import com.acme.clipcascade.utils.UserValidator;

@Service
public class FacadeUserService {

    private final Logger logger = (Logger) LoggerFactory.getLogger(FacadeUserService.class);

    private final UserService userService;
    private final UserInfoService userInfoService;
    private final ClipCascadeProperties clipCascadeProperties;
    private final IpAddressResolver ipAddressResolver;

    public FacadeUserService(
            UserService userService,
            UserInfoService userInfoService,
            ClipCascadeProperties clipCascadeProperties,
            IpAddressResolver ipAddressResolver) {

        this.userService = userService;
        this.userInfoService = userInfoService;
        this.clipCascadeProperties = clipCascadeProperties;
        this.ipAddressResolver = ipAddressResolver;
    }

    /**
     * Bootstraps the initial admin account from CC_ADMIN_USERNAME /
     * CC_ADMIN_PASSWORD, and only when the user database is completely empty. An
     * existing database is never touched: the admin credential (or any other
     * account) is never reset or recreated on restart. Startup fails fast when
     * the admin credentials are missing (ProductionConfigValidator).
     */
    public void insertDefaultAdminUserIfEmpty() {
        if (userService.isTableEmpty()) {
            String adminUsername = clipCascadeProperties.getAdminUsername();

            userService.doubleHashAndCreateUser(
                    adminUsername,
                    clipCascadeProperties.getAdminPassword(),
                    RoleConstants.ADMIN,
                    true);

            userInfoService.registerNewUser(adminUsername);

            logger.info("Bootstrapped initial admin user '{}'", adminUsername); // metadata only, never the password
        }
    }

    public Users registerUser(Users user) {
        if (!UserValidator.isValid(user)) {
            return null;
        }

        /*
         * Always perform the expensive password hash BEFORE the existence
         * check, so that response timing cannot reveal whether the username
         * is already taken (signup username enumeration via timing).
         */
        String hashedPassword = userService.hashPasswordForStorage(user.getPassword());

        if (userService.userExists(user.getUsername())
                || userInfoService.userExists(user.getUsername())) {
            return null;
        }

        long maxUserAccounts = clipCascadeProperties.getMaxUserAccounts();
        if (maxUserAccounts != -1
                && userService.countUsers() >= maxUserAccounts) {
            return null;
        }

        userInfoService.registerNewUser(user.getUsername());

        user.setPassword(hashedPassword);
        return userService.createUser(user);
    }

    public Users updateUsername(
            String oldUsername,
            String newUsername,
            String principalUsername,
            SessionService sessionService) {

        if (!UserValidator.isValidUsername(oldUsername)
                || !UserValidator.isValidUsername(newUsername)
                || userService.userExists(newUsername)
                || userInfoService.userExists(newUsername)) {

            return null;
        }

        sessionService.logoutAllSessions(oldUsername);

        UserInfo userInfo = userInfoService.markUserForDeletion(oldUsername);
        if (userInfo == null) {
            userInfoService.registerNewUser(newUsername);
        } else {
            userInfo.setUsername(newUsername);
            userInfoService.registerNewUser(userInfo);
        }

        return userService.updateUsername(oldUsername, newUsername);
    }

    public boolean deleteUser(String username, SessionService sessionService) {
        if (!UserValidator.isValidUsername(username)
                || !userService.userExists(username)) {

            return false;
        }

        sessionService.logoutAllSessions(username);

        userInfoService.markUserForDeletion(username);

        return userService.deleteUser(username);
    }

    /**
     * Self-service password change: the caller must prove knowledge of the
     * current password before it can be replaced.
     */
    public Users updatePasswordWithVerification(
            String username,
            String oldPassword,
            String newPassword) {

        if (!UserValidator.isValidUsername(username)
                || !UserValidator.isValidPassword(newPassword)
                || !userService.verifyPassword(username, oldPassword)) {

            return null;
        }

        return updatePasswordInternal(username, newPassword);
    }

    /**
     * Admin-initiated reset of another user's password (no old password
     * required; the endpoint is already admin-gated).
     */
    public Users updatePassword(String username, String newPassword) {
        if (!UserValidator.isValidUsername(username)
                || !UserValidator.isValidPassword(newPassword)) {

            return null;
        }

        return updatePasswordInternal(username, newPassword);
    }

    private Users updatePasswordInternal(String username, String newPassword) {
        userInfoService.setPasswordChangeTime(username, TimeUtility.getCurrentTimeInSeconds());

        return userService.updatePassword(username, newPassword);
    }

    public Users updateUserStatus(
            String username,
            boolean enable,
            SessionService sessionService) {

        if (!UserValidator.isValidUsername(username)
                || !userService.userExists(username)) {

            return null;
        }

        sessionService.logoutAllSessions(username);

        return userService.updateUserStatus(username, enable);
    }

    public UserInfo setLoginDetails(String username, IpAttemptDetails ipDetails) {
        int lockCount = ipDetails.getLockCount();
        if (ipDetails.getAttempts() == 0) {
            lockCount -= 1;
        }
        String lockoutTime = TimeUtility.convertSecondsToString(
                clipCascadeProperties.getLockTimeoutSeconds()
                        * (lockCount * clipCascadeProperties.getLockTimeoutScalingFactor()));

        return userInfoService.setLoginDetails(
                username,
                ipAddressResolver.getUserIpAddress(),
                TimeUtility.getCurrentTimeInSeconds(),
                (ipDetails.getAttempts() + (ipDetails.getLockCount() * clipCascadeProperties.getMaxAttemptsPerIp()))
                        - 1,
                lockoutTime);
    }

    public void deleteInactiveUsers(SessionService sessionService, Set<Users> excludedUsers) {
        if (clipCascadeProperties.getAccountPurgeTimeoutSeconds() < 0) {
            return;
        }

        Set<String> inactiveUsers = userInfoService.getInactiveUsers(
                clipCascadeProperties.getAccountPurgeTimeoutSeconds());

        // Remove excluded users from inactiveUsers
        Set<String> excludedUserIds = excludedUsers.stream()
                .map(Users::getUsername)
                .collect(Collectors.toSet());
        inactiveUsers.removeAll(excludedUserIds);

        for (String inactiveUser : inactiveUsers) {
            deleteUser(inactiveUser, sessionService);
        }
    }
}

package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.awt.image.BufferedImage;
import java.security.SecureRandom;

import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpSession;
import org.springframework.test.util.ReflectionTestUtils;

import com.acme.clipcascade.config.ClipCascadeProperties;
import com.acme.clipcascade.service.CaptchaService;

/**
 * Captcha hardening: cryptographically secure randomness and null-safe
 * validation (a missing input must fail validation, not throw).
 */
class CaptchaServiceTests {

    private CaptchaService serviceWithSignup(boolean enabled) {
        ClipCascadeProperties properties = new ClipCascadeProperties();
        ReflectionTestUtils.setField(properties, "signupEnabled", enabled);
        return new CaptchaService(properties);
    }

    @Test
    void randomnessComesFromSecureRandom() {
        Object random = ReflectionTestUtils.getField(serviceWithSignup(true), "RANDOM");

        assertNotNull(random);
        assertEquals(SecureRandom.class, random.getClass(),
                "captcha randomness must come from SecureRandom, not java.util.Random");
    }

    @Test
    void validateCaptchaAcceptsCorrectAnswer() {
        MockHttpSession session = new MockHttpSession();
        session.setAttribute("captcha-id", "ab12");

        assertTrue(serviceWithSignup(true).validateCaptcha("ab12", true, session, "captcha-id"));
    }

    @Test
    void validateCaptchaRejectsWrongAnswer() {
        MockHttpSession session = new MockHttpSession();
        session.setAttribute("captcha-id", "ab12");

        assertFalse(serviceWithSignup(true).validateCaptcha("zz99", true, session, "captcha-id"));
    }

    @Test
    void validateCaptchaIsCaseInsensitiveWhenConfigured() {
        MockHttpSession session = new MockHttpSession();
        session.setAttribute("captcha-id", "ab12");

        assertTrue(serviceWithSignup(true).validateCaptcha("AB12", false, session, "captcha-id"));
    }

    @Test
    void validateCaptchaWithNullInputFailsInsteadOfThrowing() {
        MockHttpSession session = new MockHttpSession();
        session.setAttribute("captcha-id", "ab12");

        assertFalse(serviceWithSignup(true).validateCaptcha(null, false, session, "captcha-id"));
    }

    @Test
    void validateCaptchaWithoutStoredAnswerFails() {
        MockHttpSession session = new MockHttpSession();

        assertFalse(serviceWithSignup(true).validateCaptcha("ab12", true, session, "captcha-id"));
    }

    @Test
    void validateCaptchaConsumesTheStoredAnswer() {
        MockHttpSession session = new MockHttpSession();
        session.setAttribute("captcha-id", "ab12");
        CaptchaService service = serviceWithSignup(true);

        assertTrue(service.validateCaptcha("ab12", true, session, "captcha-id"));
        // replaying the same answer must fail: single use
        assertFalse(service.validateCaptcha("ab12", true, session, "captcha-id"));
    }

    @Test
    void generateCaptchaStoresAnswerInSessionAndReturnsImage() {
        MockHttpSession session = new MockHttpSession();
        CaptchaService service = serviceWithSignup(true);

        BufferedImage image = service.generateCaptcha(200, 50, 5, 6, session, "captcha-id", false);

        assertNotNull(image);
        assertNotNull(session.getAttribute("captcha-id"));
    }
}

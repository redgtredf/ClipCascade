package com.acme.ClipCascade;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

// @SpringBootTest
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test") // dummy secrets + explicit origins (see application-test.properties)
class ClipCascadeApplicationTests {

	@Test
	void contextLoads() {
	}

}

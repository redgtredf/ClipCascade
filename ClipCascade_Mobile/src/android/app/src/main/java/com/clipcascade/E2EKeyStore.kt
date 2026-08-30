package com.clipcascade

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class E2EKeyStore(context: Context) {
    companion object {
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val KEY_ALIAS = "clipcascade_e2e_wrapping_key_v1"
        private const val PREFS_NAME = "clipcascade_secure_keys"
        private const val WRAPPED_KEY = "wrapped_e2e_key"
        private const val WRAPPED_KEY_IV = "wrapped_e2e_key_iv"
        private const val E2E_KEY_BYTES = 32
    }

    private val preferences = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    @Synchronized
    fun store(base64Key: String) {
        val keyBytes = decodeAndValidate(base64Key)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateWrappingKey())
        val ciphertext = cipher.doFinal(keyBytes)

        check(preferences.edit()
            .putString(WRAPPED_KEY, Base64.encodeToString(ciphertext, Base64.NO_WRAP))
            .putString(WRAPPED_KEY_IV, Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .commit()) { "Failed to persist the wrapped E2E key" }
    }

    @Synchronized
    fun retrieve(): String? {
        val wrapped = preferences.getString(WRAPPED_KEY, null) ?: return null
        val iv = preferences.getString(WRAPPED_KEY_IV, null) ?: return null
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(
            Cipher.DECRYPT_MODE,
            getExistingWrappingKey(),
            GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP)),
        )
        val plaintext = cipher.doFinal(Base64.decode(wrapped, Base64.NO_WRAP))
        check(plaintext.size == E2E_KEY_BYTES) { "Stored E2E key has an invalid length" }
        return Base64.encodeToString(plaintext, Base64.NO_WRAP)
    }

    @Synchronized
    fun clear() {
        check(preferences.edit().remove(WRAPPED_KEY).remove(WRAPPED_KEY_IV).commit()) {
            "Failed to clear the wrapped E2E key"
        }
    }

    private fun decodeAndValidate(base64Key: String): ByteArray {
        val decoded = try {
            Base64.decode(base64Key, Base64.NO_WRAP)
        } catch (error: IllegalArgumentException) {
            throw IllegalArgumentException("E2E key must be valid base64", error)
        }
        require(decoded.size == E2E_KEY_BYTES) { "E2E key must be exactly 32 bytes" }
        return decoded
    }

    private fun getExistingWrappingKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        return keyStore.getKey(KEY_ALIAS, null) as? SecretKey
            ?: throw IllegalStateException("Android Keystore wrapping key is missing")
    }

    private fun getOrCreateWrappingKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }

        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE).run {
            init(
                KeyGenParameterSpec.Builder(
                    KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .build(),
            )
            generateKey()
        }
    }
}

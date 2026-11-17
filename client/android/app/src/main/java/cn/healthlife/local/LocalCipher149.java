package cn.healthlife.local;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import javax.crypto.Cipher;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Authenticated local records. The Android boundary owns the non-exportable key. */
final class LocalCipher149 {
    static final int MAX_BYTES = 32 * 1024 * 1024;
    private LocalCipher149() { }

    static byte[] aad(long actor, String purpose) {
        if (actor <= 0 || purpose == null || purpose.length() < 1 || purpose.length() > 500
                || purpose.indexOf('\0') >= 0) throw new IllegalArgumentException("Invalid local scope");
        return ("FAWNSEE/local/v149/" + actor + "/" + purpose).getBytes(StandardCharsets.UTF_8);
    }

    static byte[] seal(SecretKey key, byte[] plaintext, byte[] aad) throws GeneralSecurityException {
        if (plaintext == null || plaintext.length > MAX_BYTES) throw new IllegalArgumentException("Local record too large");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key);
        cipher.updateAAD(aad);
        byte[] iv = cipher.getIV(), body = cipher.doFinal(plaintext);
        if (iv.length != 12) throw new GeneralSecurityException("Unsupported local nonce");
        byte[] output = new byte[iv.length + body.length];
        System.arraycopy(iv, 0, output, 0, iv.length);
        System.arraycopy(body, 0, output, iv.length, body.length);
        return output;
    }

    static byte[] open(SecretKey key, byte[] ciphertext, byte[] aad) throws GeneralSecurityException {
        if (ciphertext == null || ciphertext.length < 28 || ciphertext.length > MAX_BYTES + 28)
            throw new IllegalArgumentException("Invalid local record");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key, new GCMParameterSpec(128, ciphertext, 0, 12));
        cipher.updateAAD(aad);
        return cipher.doFinal(ciphertext, 12, ciphertext.length - 12);
    }
}

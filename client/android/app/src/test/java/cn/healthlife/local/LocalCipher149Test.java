package cn.healthlife.local;

import org.junit.Test;
import static org.junit.Assert.*;
import java.nio.charset.StandardCharsets;
import javax.crypto.spec.SecretKeySpec;

public final class LocalCipher149Test {
    private final SecretKeySpec key = new SecretKeySpec(new byte[32], "AES");
    private final byte[] plain = "用户饮水记录，完整保存。".getBytes(StandardCharsets.UTF_8);
    @Test public void roundtripHasFreshNonceAndExactContent() throws Exception {
        byte[] aad = LocalCipher149.aad(59, "endpoint/record/uuid");
        byte[] first = LocalCipher149.seal(key, plain, aad), second = LocalCipher149.seal(key, plain, aad);
        assertFalse(java.util.Arrays.equals(first, second));
        assertArrayEquals(plain, LocalCipher149.open(key, first, aad));
    }
    @Test public void anotherAccountOrPurposeCannotReadRecord() throws Exception {
        byte[] value = LocalCipher149.seal(key, plain, LocalCipher149.aad(59, "cloud/one"));
        for (byte[] aad : new byte[][]{LocalCipher149.aad(62,"cloud/one"), LocalCipher149.aad(59,"cloud/two")}) {
            try { LocalCipher149.open(key,value,aad); fail("A changed scope was accepted"); }
            catch (javax.crypto.AEADBadTagException expected) { }
        }
    }
    @Test public void tamperAndTruncationAreRejected() throws Exception {
        byte[] aad = LocalCipher149.aad(59,"record"), value=LocalCipher149.seal(key,plain,aad);
        value[value.length-1]^=1;
        try { LocalCipher149.open(key,value,aad);fail("Tampering accepted"); }
        catch (javax.crypto.AEADBadTagException expected) { }
        try { LocalCipher149.open(key,new byte[27],aad);fail("Truncation accepted"); }
        catch (IllegalArgumentException expected) { }
    }
    @Test public void scopeAndResourceBoundsAreEnforced() throws Exception {
        try { LocalCipher149.aad(0,"record");fail(); } catch(IllegalArgumentException expected) { }
        try { LocalCipher149.aad(1,"");fail(); } catch(IllegalArgumentException expected) { }
        try { LocalCipher149.seal(key,new byte[LocalCipher149.MAX_BYTES+1],LocalCipher149.aad(1,"x"));fail(); }
        catch(IllegalArgumentException expected) { }
    }
}

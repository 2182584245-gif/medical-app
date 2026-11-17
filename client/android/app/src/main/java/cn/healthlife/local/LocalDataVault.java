package cn.healthlife.local;

import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;

/** Python-only boundary. Never installed as a JavascriptInterface or exports key material. */
public final class LocalDataVault {
    private static final String PREFIX = "fawnsee.local149.actor.";
    private static final String CONTROL = "fawnsee.client-erasure.control.v1";

    static synchronized void clearAllActorKeys() throws Exception {
        KeyStore store=KeyStore.getInstance("AndroidKeyStore");store.load(null);
        java.util.Enumeration<String> aliases=store.aliases();
        java.util.List<String> names=java.util.Collections.list(aliases);
        for(String alias:names) {
            if(alias.equals(CONTROL) || alias.matches(java.util.regex.Pattern.quote(PREFIX)+"[1-9][0-9]*"))store.deleteEntry(alias);
        }
    }
    public void clearActorKey(long actor) throws Exception {
        if(actor<=0)throw new IllegalArgumentException("Invalid local account");
        synchronized(LocalDataVault.class) {
            KeyStore store=KeyStore.getInstance("AndroidKeyStore");store.load(null);store.deleteEntry(PREFIX+actor);
        }
    }

    private static synchronized SecretKey key(long actor, boolean create) throws Exception {
        if (actor <= 0) throw new IllegalArgumentException("Invalid local account");
        return keyAlias(PREFIX+actor,create);
    }
    private static synchronized SecretKey keyAlias(String alias,boolean create) throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (!store.containsAlias(alias)) {
            if (!create) throw new IllegalStateException("Local key unavailable");
            KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(alias,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true).setKeySize(256).build());
            generator.generateKey();
        }
        KeyStore.Entry entry = store.getEntry(alias, null);
        if (!(entry instanceof KeyStore.SecretKeyEntry)) throw new IllegalStateException("Local key unavailable");
        return ((KeyStore.SecretKeyEntry) entry).getSecretKey();
    }

    public String seal(long actor, String purpose, String plaintext) throws Exception {
        byte[] aad = LocalCipher149.aad(actor, purpose);
        if (plaintext == null || plaintext.length() > LocalCipher149.MAX_BYTES) throw new IllegalArgumentException();
        byte[] bytes = plaintext.getBytes(StandardCharsets.UTF_8);
        return "v149." + Base64.encodeToString(LocalCipher149.seal(key(actor,true), bytes, aad), Base64.NO_WRAP);
    }

    public String open(long actor, String purpose, String ciphertext) throws Exception {
        byte[] aad = LocalCipher149.aad(actor, purpose);
        if (ciphertext == null || !ciphertext.startsWith("v149.")
                || ciphertext.length() > (LocalCipher149.MAX_BYTES + 30L) * 4 / 3 + 16) throw new IllegalArgumentException();
        byte[] bytes = Base64.decode(ciphertext.substring(5), Base64.NO_WRAP);
        return new String(LocalCipher149.open(key(actor,false), bytes, aad), StandardCharsets.UTF_8);
    }
    private static String guestAlias(String scope) {
        if(scope==null || !scope.matches("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"))throw new IllegalArgumentException("Invalid guest scope");
        return "fawnsee.guest11614."+scope;
    }
    private static byte[] guestAad(String scope,String purpose) {
        guestAlias(scope);
        if(purpose==null || purpose.isEmpty() || purpose.length()>500 || purpose.indexOf('\0')>=0)throw new IllegalArgumentException("Invalid guest purpose");
        return ("FAWNSEE/guest/v11614/"+scope+"/"+purpose).getBytes(StandardCharsets.UTF_8);
    }
    public String sealGuest(String scope,String purpose,String plaintext) throws Exception {
        if(plaintext==null || plaintext.length()>LocalCipher149.MAX_BYTES)throw new IllegalArgumentException();
        return "g11614."+Base64.encodeToString(LocalCipher149.seal(keyAlias(guestAlias(scope),true),plaintext.getBytes(StandardCharsets.UTF_8),guestAad(scope,purpose)),Base64.NO_WRAP);
    }
    public String openGuest(String scope,String purpose,String ciphertext) throws Exception {
        if(ciphertext==null || !ciphertext.startsWith("g11614.") || ciphertext.length()>(LocalCipher149.MAX_BYTES+30L)*4/3+16)throw new IllegalArgumentException();
        return new String(LocalCipher149.open(keyAlias(guestAlias(scope),false),Base64.decode(ciphertext.substring(7),Base64.NO_WRAP),guestAad(scope,purpose)),StandardCharsets.UTF_8);
    }
    public void clearGuestKey(String scope) throws Exception {
        synchronized(LocalDataVault.class) {
            KeyStore store=KeyStore.getInstance("AndroidKeyStore");store.load(null);store.deleteEntry(guestAlias(scope));
        }
    }

    public String sealControl(String purpose,String plaintext) throws Exception {
        if(plaintext==null || plaintext.length()>32000)throw new IllegalArgumentException();
        return "v149."+Base64.encodeToString(LocalCipher149.seal(keyAlias(CONTROL,true),
            plaintext.getBytes(StandardCharsets.UTF_8),LocalCipher149.aad(1,purpose)),Base64.NO_WRAP);
    }
    public String openControl(String purpose,String ciphertext) throws Exception {
        if(ciphertext==null || !ciphertext.startsWith("v149.") || ciphertext.length()>50000)throw new IllegalArgumentException();
        return new String(LocalCipher149.open(keyAlias(CONTROL,false),Base64.decode(ciphertext.substring(5),Base64.NO_WRAP),
            LocalCipher149.aad(1,purpose)),StandardCharsets.UTF_8);
    }
}

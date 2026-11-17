package cn.healthlife.local;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;
import static org.junit.Assert.*;

/** Synthetic runtime files; no Android, Python, network, account or real app data. */
public final class PythonBootstrapIntegrityTest {
    @Rule public TemporaryFolder temporary=new TemporaryFolder();
    private static byte[] bytes(String value) {return value.getBytes(StandardCharsets.UTF_8);}
    private static String sha1(byte[] bytes) throws Exception {
        StringBuilder result=new StringBuilder();
        for(byte value:MessageDigest.getInstance("SHA-1").digest(bytes))result.append(String.format(java.util.Locale.ROOT,"%02x",value&255));
        return result.toString();
    }
    private static void write(File file,byte[] contents) throws IOException {
        File parent=file.getParentFile();
        if(!parent.isDirectory() && !parent.mkdirs())throw new IOException("Fixture directory unavailable");
        Files.write(file.toPath(),contents);
    }
    private static void unavailable(IoAction action) throws Exception {
        try {action.run();fail("Invalid runtime reached native initialization");}
        catch(IOException expected) {assertEquals("Python bootstrap unavailable",expected.getMessage());}
    }
    private interface IoAction {void run() throws Exception;}

    private final class Fixture {
        final File files=temporary.newFolder(),cache=temporary.newFolder();
        final Map<String,byte[]> packed=new LinkedHashMap<>();
        final Map<String,String> manifest=new LinkedHashMap<>(),preferences=new LinkedHashMap<>();
        final Set<String> invalidated=new LinkedHashSet<>();
        final Set<File> links=new LinkedHashSet<>();
        int commits,extracts;
        boolean commit=true,rewrite=true,sdkFails;
        Fixture() throws Exception {
            add("stdlib-common.imy");add("bootstrap.imy");add("cacert.pem");add("stdlib-x86_64.imy");
            add("stdlib-arm64-v8a.imy");add("bootstrap-native/arm64-v8a/math.so");
            for(String name:new String[]{"_bz2","_ctypes","_datetime","_lzma","_opcode","_random","_struct","binascii","math","mmap","zlib","java/chaquopy"})add("bootstrap-native/x86_64/"+name+".so");
            add("app.imy");add("requirements-common.imy");
        }
        void add(String path) throws Exception {
            byte[] content=bytes("packaged bootstrap: "+path);packed.put(path,content);manifest.put(path,sha1(content));
        }
        PythonBootstrapIntegrity.Selection selection() throws IOException {
            return PythonBootstrapIntegrity.select(manifest,new String[]{"x86_64","arm64-v8a"});
        }
        File file(String path) {return new File(new File(files,"chaquopy"),path);}
        void seed() throws Exception {
            for(String path:selection().assets.keySet()) {
                write(file(path),packed.get(path));preferences.put("asset."+path,manifest.get(path));
            }
        }
        PythonBootstrapIntegrity.Result run() throws Exception {
            return PythonBootstrapIntegrity.prepare(files,cache,selection(),path->new ByteArrayInputStream(packed.get(path)),
                path->links.contains(path.getAbsoluteFile()),keys->{
                    commits++;invalidated.addAll(keys);
                    if(commit)for(String key:keys)preferences.remove(key);
                    return commit;
                },()->{
                    extracts++;
                    if(sdkFails)throw new IOException("Synthetic SDK failure");
                    if(rewrite)for(String path:selection().assets.keySet()) {
                        // The real SDK skips an existing file solely because its
                        // saved asset hash matches, even when its bytes are zero.
                        if(!file(path).exists() || !manifest.get(path).equals(preferences.get("asset."+path))) {
                            write(file(path),packed.get(path));preferences.put("asset."+path,manifest.get(path));
                        }
                    }
                    return "synthetic-bootstrap-path";
                });
        }
    }

    @Test public void validRuntimeUsesSdkWithoutInvalidatingAnyHash() throws Exception {
        Fixture fixture=new Fixture();fixture.seed();Map<String,String> before=new LinkedHashMap<>(fixture.preferences);
        PythonBootstrapIntegrity.Result result=fixture.run();
        assertEquals("synthetic-bootstrap-path",result.path);assertFalse(result.repaired);
        assertEquals(0,fixture.commits);assertEquals(1,fixture.extracts);assertEquals(before,fixture.preferences);
    }
    @Test public void zeroByteBootstrapWithAllMatchingSavedHashesRecoversFourteenAssets() throws Exception {
        Fixture fixture=new Fixture();fixture.seed();Set<String> expected=new LinkedHashSet<>();
        for(String path:fixture.selection().assets.keySet())if(!path.equals("cacert.pem")) {
            write(fixture.file(path),new byte[0]);expected.add("asset."+path);
            assertEquals(fixture.manifest.get(path),fixture.preferences.get("asset."+path));
        }
        PythonBootstrapIntegrity.Result result=fixture.run();
        assertTrue(result.repaired);assertEquals(14,expected.size());assertEquals(expected,fixture.invalidated);
        assertEquals(1,fixture.commits);assertEquals(1,fixture.extracts);
        for(String path:fixture.selection().assets.keySet())assertArrayEquals(fixture.packed.get(path),Files.readAllBytes(fixture.file(path).toPath()));
    }
    @Test public void sameSizeTamperingInvalidatesOnlyThatAsset() throws Exception {
        Fixture fixture=new Fixture();fixture.seed();String path="bootstrap.imy";
        byte[] tampered=fixture.packed.get(path).clone();tampered[0]^=1;write(fixture.file(path),tampered);
        assertTrue(fixture.run().repaired);assertEquals(Collections.singleton("asset."+path),fixture.invalidated);
        assertArrayEquals(fixture.packed.get(path),Files.readAllBytes(fixture.file(path).toPath()));
    }
    @Test public void missingFilesUseSdkNormalExtractionWithoutClearingSavedHashes() throws Exception {
        Fixture fixture=new Fixture();
        for(String path:fixture.selection().assets.keySet())fixture.preferences.put("asset."+path,fixture.manifest.get(path));
        assertFalse(fixture.run().repaired);assertEquals(0,fixture.commits);assertEquals(1,fixture.extracts);
        for(String path:fixture.selection().assets.keySet())assertTrue(fixture.file(path).isFile());
    }
    @Test public void repairPreservesMemberKeysEpochUnselectedRuntimeAndOtherPreferences() throws Exception {
        Fixture fixture=new Fixture();fixture.seed();
        File member=new File(fixture.files,"member/local-session"),keys=new File(fixture.files,"keys/sealed"),epoch=new File(fixture.files,"client-account-epoch");
        for(File file:Arrays.asList(member,keys,epoch,fixture.file("app.imy"),fixture.file("requirements-common.imy")))write(file,bytes("preserve"));
        fixture.preferences.put("asset.app.imy","app-pin");fixture.preferences.put("asset.requirements-common.imy","requirements-pin");fixture.preferences.put("unrelated","setting");
        write(fixture.file("stdlib-common.imy"),new byte[0]);assertTrue(fixture.run().repaired);
        assertEquals(Collections.singleton("asset.stdlib-common.imy"),fixture.invalidated);
        for(File file:Arrays.asList(member,keys,epoch,fixture.file("app.imy"),fixture.file("requirements-common.imy")))assertArrayEquals(bytes("preserve"),Files.readAllBytes(file.toPath()));
        assertEquals("app-pin",fixture.preferences.get("asset.app.imy"));assertEquals("requirements-pin",fixture.preferences.get("asset.requirements-common.imy"));assertEquals("setting",fixture.preferences.get("unrelated"));
    }
    @Test public void failedHashCommitStopsBeforeSdkOrNativeStartup() throws Exception {
        Fixture fixture=new Fixture();fixture.seed();write(fixture.file("bootstrap.imy"),new byte[0]);fixture.commit=false;
        unavailable(()->fixture.run());assertEquals(1,fixture.commits);assertEquals(0,fixture.extracts);assertEquals(0,fixture.file("bootstrap.imy").length());
    }
    @Test public void sdkReturningWithoutRepairCannotReachNativeInitialization() throws Exception {
        Fixture fixture=new Fixture();fixture.seed();write(fixture.file("bootstrap.imy"),new byte[0]);fixture.rewrite=false;
        unavailable(()->fixture.run());assertEquals(1,fixture.extracts);assertEquals(Collections.singleton("asset.bootstrap.imy"),fixture.invalidated);
    }
    @Test public void sdkLeavingMissingFilesCannotReachNativeInitialization() throws Exception {
        Fixture fixture=new Fixture();fixture.rewrite=false;
        unavailable(()->fixture.run());assertEquals(0,fixture.commits);assertEquals(1,fixture.extracts);
    }
    @Test public void invalidPackagedHashFailsBeforePreferenceOrSdkChanges() throws Exception {
        Fixture fixture=new Fixture();fixture.seed();fixture.packed.put("bootstrap.imy",bytes("damaged package"));
        unavailable(()->fixture.run());assertEquals(0,fixture.commits);assertEquals(0,fixture.extracts);
    }
    @Test public void traversalAbsoluteAndMalformedManifestPathsAreRejected() throws Exception {
        for(String path:new String[]{"../member/session","/bootstrap.imy","nested//file","nested/./file","nested\\file","C:/member","bootstrap-native/x86_64/../member"}) {
            Fixture fixture=new Fixture();fixture.manifest.put(path,fixture.manifest.get("bootstrap.imy"));
            unavailable(()->fixture.run());assertEquals(0,fixture.commits);assertEquals(0,fixture.extracts);
        }
    }
    @Test public void sdkSelectionUsesFirstPackagedDeviceAbiAndExcludesOtherAbiAndLazyArchives() throws Exception {
        Fixture fixture=new Fixture();PythonBootstrapIntegrity.Selection selected=PythonBootstrapIntegrity.select(fixture.manifest,new String[]{"unsupported","arm64-v8a","x86_64"});
        assertEquals("arm64-v8a",selected.abi);assertEquals(4,selected.assets.size());
        assertTrue(selected.assets.containsKey("bootstrap-native/arm64-v8a/math.so"));assertFalse(selected.assets.containsKey("app.imy"));assertFalse(selected.assets.containsKey("stdlib-arm64-v8a.imy"));
    }
    @Test public void linkedBootstrapLeafOrAncestorOrTemporaryFailsBeforeAnyMutation() throws Exception {
        for(String path:new String[]{"bootstrap.imy","bootstrap-native","bootstrap-native/x86_64","bootstrap.imy.tmp"}) {
            Fixture fixture=new Fixture();fixture.seed();fixture.links.add(fixture.file(path).getAbsoluteFile());
            unavailable(()->fixture.run());assertEquals(0,fixture.commits);assertEquals(0,fixture.extracts);
        }
    }
    @Test public void unknownNativeTreeLinkAndSdkObsoleteLinksCannotBeFollowedBySdkCleanup() throws Exception {
        for(String path:new String[]{"bootstrap-native/x86_64/unknown-link","lib-dynload/nested-link","cache-AssetFinder"}) {
            Fixture fixture=new Fixture();fixture.seed();File node=path.equals("cache-AssetFinder")?new File(fixture.cache,"chaquopy/AssetFinder/nested-link"):fixture.file(path);
            write(node,bytes("external sentinel"));fixture.links.add(node.getAbsoluteFile());
            unavailable(()->fixture.run());assertEquals(0,fixture.commits);assertEquals(0,fixture.extracts);
            assertArrayEquals(bytes("external sentinel"),Files.readAllBytes(node.toPath()));
        }
    }
    @Test public void runtimeDirectoryAndWrongTypeAssetFailWithoutSdkDeletion() throws Exception {
        Fixture fixture=new Fixture();assertTrue(fixture.file("bootstrap.imy").mkdirs());
        unavailable(()->fixture.run());assertTrue(fixture.file("bootstrap.imy").isDirectory());assertEquals(0,fixture.extracts);
    }
}

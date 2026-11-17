package cn.healthlife.local;

import org.junit.Test;
import static org.junit.Assert.*;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InterruptedIOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

/** Policy tests run on the JVM without Android, networking, or member fixtures. */
public class ReleaseInfoTest {
    private Map<String,Object> manifest() {
        Map<String,Object> value = new HashMap<>();
        value.put("schema_version",1); value.put("application_id","com.fawnsee.mobile");
        value.put("version_name","1.1.3"); value.put("version_code",10134); value.put("min_sdk",24);
        value.put("size_bytes",3L); value.put("sha256","a".repeat(64));
        value.put("apk_url","https://39.106.166.15/aliyun/mobile-updates/FAWNSEE-MOBILE-10134.apk");
        value.put("published_at","2026-10-25T14:00:00Z"); value.put("release_notes","应用体验更新");
        return value;
    }
    private void rejected(Map<String,Object> value) throws Exception {
        try { ReleaseInfo.parse(value,"com.fawnsee.mobile"); fail("Unsafe manifest accepted"); }
        catch (IOException expected) { }
    }
    private String digest(byte[] bytes) throws Exception { return ReleaseInfo.hex(MessageDigest.getInstance("SHA-256").digest(bytes)); }

    @Test public void acceptsExactReleaseContract() throws Exception {
        ReleaseInfo info = ReleaseInfo.parse(manifest(),"com.fawnsee.mobile");
        assertEquals(10134,info.versionCode); assertEquals(3,info.sizeBytes); assertEquals(24,info.minSdk);
    }
    @Test public void refusesWrongPackageAndUnknownOrMissingFields() throws Exception {
        Map<String,Object> value=manifest(); value.put("application_id","com.other.mobile"); rejected(value);
        value=manifest(); value.put("untrusted_url","https://invalid.invalid/"); rejected(value);
        value=manifest(); value.remove("sha256"); rejected(value);
    }
    @Test public void refusesSchemaChangesAndNonIntegerNumbers() throws Exception {
        for (Object bad : new Object[]{2,1.0,"1",null}) { Map<String,Object> value=manifest(); value.put("schema_version",bad); rejected(value); }
        for (Object bad : new Object[]{10134.0,"10134",0,-1,2147483648L}) { Map<String,Object> value=manifest(); value.put("version_code",bad); rejected(value); }
    }
    @Test public void boundsArtifactSizeAndMinimumSdk() throws Exception {
        for (Object bad : new Object[]{0,-1,ReleaseInfo.MAX_APK_BYTES+1,Double.NaN,"3"}) { Map<String,Object> value=manifest(); value.put("size_bytes",bad); rejected(value); }
        Map<String,Object> value=manifest(); value.put("min_sdk",0); rejected(value);
    }
    @Test public void rejectsHashAndUnboundedOrEmptyText() throws Exception {
        for (String bad : new String[]{"A".repeat(64),"a".repeat(63),"0".repeat(65),"g".repeat(64)}) { Map<String,Object> value=manifest(); value.put("sha256",bad); rejected(value); }
        for (String bad : new String[]{"", " update", "update ", "\0", "x".repeat(8001)}) { Map<String,Object> value=manifest(); value.put("release_notes",bad); rejected(value); }
    }
    @Test public void rejectsAlternateUrlDestinationsAndEncodings() throws Exception {
        for (String url : new String[]{
                "http://39.106.166.15/aliyun/mobile-updates/FAWNSEE-MOBILE-10134.apk",
                "https://39.106.166.15:443/aliyun/mobile-updates/FAWNSEE-MOBILE-10134.apk",
                "https://user@39.106.166.15/aliyun/mobile-updates/FAWNSEE-MOBILE-10134.apk",
                "https://other.invalid/aliyun/mobile-updates/FAWNSEE-MOBILE-10134.apk",
                "https://39.106.166.15/aliyun/mobile-updates/FAWNSEE-MOBILE-10134.apk?token=x",
                "https://39.106.166.15/aliyun/mobile-updates/FAWNSEE-MOBILE-10134.apk#fragment",
                "https://39.106.166.15/aliyun/mobile-updates/../FAWNSEE-MOBILE-10134.apk",
                "https://39.106.166.15/aliyun/mobile-updates/FAWNSEE-MOBILE-%31%30%31%33%34.apk",
                "https://39.106.166.15/aliyun/mobile-updates/FAWNSEE-MOBILE-10135.apk"}) {
            Map<String,Object> value=manifest(); value.put("apk_url",url); rejected(value);
        }
    }
    @Test public void transportPolicyOnlyAcceptsManifestOrNumberedApk() throws Exception {
        assertNotNull(ReleaseInfo.requireDownloadUrl(ReleaseInfo.MANIFEST_URL));
        assertNotNull(ReleaseInfo.requireDownloadUrl("https://39.106.166.15/aliyun/mobile-updates/FAWNSEE-MOBILE-10134.apk"));
        for (String value : new String[]{"https://39.106.166.15/aliyun/mobile-updates/latest.json?x=1", "https://39.106.166.15/aliyun/mobile-updates/source.py", "https://39.106.166.15/aliyun/mobile-updates/FAWNSEE-MOBILE-01.apk"}) {
            try { ReleaseInfo.requireDownloadUrl(value); fail("Unpublished path accepted"); } catch (IOException expected) { }
        }
    }
    @Test public void streamsExactBytesAndReportsRealProgress() throws Exception {
        byte[] bytes="a signed package boundary".getBytes(StandardCharsets.UTF_8);
        ByteArrayOutputStream output=new ByteArrayOutputStream(); AtomicLong seen=new AtomicLong();
        assertEquals(bytes.length,ReleaseInfo.copyVerified(new ByteArrayInputStream(bytes),output,bytes.length,digest(bytes),()->false,seen::set));
        assertArrayEquals(bytes,output.toByteArray()); assertEquals(bytes.length,seen.get());
    }
    @Test public void refusesTruncationExtraBytesAndDigestMismatch() throws Exception {
        byte[] bytes="abc".getBytes(StandardCharsets.UTF_8);
        for (long exact : new long[]{2,4}) {
            try { ReleaseInfo.copyVerified(new ByteArrayInputStream(bytes),new ByteArrayOutputStream(),exact,digest(bytes),()->false,n->{}); fail("Wrong length accepted"); }
            catch (IOException expected) { }
        }
        try { ReleaseInfo.copyVerified(new ByteArrayInputStream(bytes),new ByteArrayOutputStream(),3,"f".repeat(64),()->false,n->{}); fail("Wrong digest accepted"); }
        catch (IOException expected) { }
    }
    @Test public void cancellationStopsBeforeAnyFurtherBytes() throws Exception {
        ByteArrayOutputStream output=new ByteArrayOutputStream();
        try { ReleaseInfo.copyVerified(new ByteArrayInputStream(new byte[]{1,2,3}),output,3,"f".repeat(64),()->true,n->fail("Progress after cancellation")); fail("Cancelled copy completed"); }
        catch (InterruptedIOException expected) { assertEquals(0,output.size()); }
    }
    @Test public void requiresWholeCurrentSignerSetRegardlessOfOrder() {
        byte[] a={1,2},b={3,4},c={5,6};
        assertTrue(ReleaseInfo.sameSigners(new byte[][]{a,b},new byte[][]{b,a}));
        assertFalse(ReleaseInfo.sameSigners(new byte[][]{a,b},new byte[][]{a,c}));
        assertFalse(ReleaseInfo.sameSigners(new byte[][]{a,b},new byte[][]{a}));
        assertFalse(ReleaseInfo.sameSigners(new byte[][]{a,a},new byte[][]{a,a}));
        assertFalse(ReleaseInfo.sameSigners(null,new byte[][]{a}));
        assertFalse(ReleaseInfo.sameSigners(new byte[][]{},new byte[][]{}));
        assertFalse(ReleaseInfo.sameSigners(new byte[][]{null},new byte[][]{null}));
    }

    @Test public void readableApkProbeAcceptsKnownAndUnknownDescriptorLengths() throws Exception {
        byte[] prefix = {0x50,0x4b,0x03,0x04,0x00};
        ReleaseInfo.requireReadableApk(new ByteArrayInputStream(prefix),142415668L,142415668L);
        ReleaseInfo.requireReadableApk(new ByteArrayInputStream(prefix),-1,142415668L);
    }
    @Test public void readableApkProbeRejectsTruncatedWrongAndMissingUris() throws Exception {
        for (byte[] prefix : new byte[][]{{},{0x50},{0x50,0x4b,0x03},{0x50,0x4b,0x05,0x06},{0x3c,0x68,0x74,0x6d}}) {
            try { ReleaseInfo.requireReadableApk(new ByteArrayInputStream(prefix),100,100); fail("Unreadable URI accepted"); }
            catch (IOException expected) { }
        }
        try { ReleaseInfo.requireReadableApk(null,100,100); fail("Missing descriptor accepted"); }
        catch (IOException expected) { }
    }
    @Test public void readableApkProbeRejectsDifferentDescriptorSizeAndInvalidBounds() throws Exception {
        byte[] prefix = {0x50,0x4b,0x03,0x04};
        for (long[] sizes : new long[][]{{99,100},{101,100},{-2,100},{4,3},{-1,ReleaseInfo.MAX_APK_BYTES+1}}) {
            try { ReleaseInfo.requireReadableApk(new ByteArrayInputStream(prefix),sizes[0],sizes[1]); fail("Wrong URI size accepted"); }
            catch (IOException expected) { }
        }
    }
}

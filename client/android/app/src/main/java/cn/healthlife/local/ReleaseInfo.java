package cn.healthlife.local;

import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.io.OutputStream;
import java.net.URI;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.function.BooleanSupplier;
import java.util.function.LongConsumer;

/** Pure update policy: no account state, network credentials, or caller-selected URLs. */
final class ReleaseInfo {
    static final String MANIFEST_URL = "https://39.106.166.15/aliyun/mobile-updates/latest.json";
    static final String PATH = "/aliyun/mobile-updates/";
    static final long MAX_APK_BYTES = 300L * 1024 * 1024;
    static final int MAX_MANIFEST_BYTES = 32 * 1024;
    final String applicationId, versionName, sha256, apkUrl, publishedAt, releaseNotes;
    final long versionCode, sizeBytes;
    final int minSdk;

    private ReleaseInfo(Map<String, Object> value) throws IOException {
        applicationId = text(value, "application_id", 150);
        versionName = text(value, "version_name", 64);
        versionCode = integer(value, "version_code", 1, Integer.MAX_VALUE);
        minSdk = (int) integer(value, "min_sdk", 1, 1000);
        sizeBytes = integer(value, "size_bytes", 1, MAX_APK_BYTES);
        sha256 = text(value, "sha256", 64);
        apkUrl = text(value, "apk_url", 300);
        publishedAt = text(value, "published_at", 64);
        releaseNotes = text(value, "release_notes", 8000);
        if (!applicationId.matches("[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z][A-Za-z0-9_]*)+")
                || !versionName.matches("[A-Za-z0-9][A-Za-z0-9.+_-]*")
                || !sha256.matches("[a-f0-9]{64}")
                || !publishedAt.matches("\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,9})?(Z|[+-]\\d{2}:\\d{2})")) {
            throw invalid();
        }
        requireUrl(apkUrl, PATH + "FAWNSEE-MOBILE-" + versionCode + ".apk");
    }

    static ReleaseInfo parse(Map<String, Object> value, String installedApplicationId) throws IOException {
        Set<String> fields = new HashSet<>(Arrays.asList("schema_version", "application_id", "version_name", "version_code",
                "min_sdk", "size_bytes", "sha256", "apk_url", "published_at", "release_notes"));
        if (value == null || !value.keySet().equals(fields) || integer(value, "schema_version", 1, 1) != 1) throw invalid();
        ReleaseInfo result = new ReleaseInfo(value);
        if (!result.applicationId.equals(installedApplicationId)) throw new IOException("更新包与当前应用不匹配。");
        return result;
    }

    static URI requireUrl(String value, String exactPath) throws IOException {
        try {
            URI uri = new URI(value);
            if (!"https".equals(uri.getScheme()) || !"39.106.166.15".equals(uri.getHost())
                    || uri.getPort() != -1 || uri.getRawUserInfo() != null || uri.getRawQuery() != null
                    || uri.getRawFragment() != null || !exactPath.equals(uri.getRawPath())) throw invalid();
            return uri;
        } catch (java.net.URISyntaxException error) { throw invalid(); }
    }

    static URI requireDownloadUrl(String value) throws IOException {
        if (MANIFEST_URL.equals(value)) return requireUrl(value, PATH + "latest.json");
        try {
            URI uri = new URI(value);
            String path = uri.getRawPath();
            if (path == null || !path.matches("/aliyun/mobile-updates/FAWNSEE-MOBILE-[1-9][0-9]{0,9}\\.apk")) throw invalid();
            return requireUrl(value, path);
        } catch (java.net.URISyntaxException error) { throw invalid(); }
    }

    static long copyVerified(InputStream input, OutputStream output, long exactBytes, String expectedSha256,
                             BooleanSupplier cancelled, LongConsumer progress) throws IOException {
        if (exactBytes < 1 || exactBytes > MAX_APK_BYTES || !expectedSha256.matches("[a-f0-9]{64}")) throw invalid();
        final MessageDigest digest;
        try { digest = MessageDigest.getInstance("SHA-256"); }
        catch (java.security.NoSuchAlgorithmException error) { throw new IOException("无法校验更新包。"); }
        byte[] buffer = new byte[32 * 1024];
        long count = 0;
        for (;;) {
            if (cancelled.getAsBoolean()) throw new InterruptedIOException("更新已取消。");
            int size = input.read(buffer);
            if (size < 0) break;
            if (size == 0) continue;
            if (size > exactBytes - count) throw new IOException("更新包长度不一致，请重新下载。");
            digest.update(buffer, 0, size);
            output.write(buffer, 0, size);
            count += size;
            progress.accept(count);
        }
        if (cancelled.getAsBoolean()) throw new InterruptedIOException("更新已取消。");
        if (count != exactBytes || !hex(digest.digest()).equals(expectedSha256)) throw new IOException("更新包校验未通过，请重新下载。");
        return count;
    }

    static boolean sameSigners(byte[][] installed, byte[][] archive) {
        if (installed == null || archive == null || installed.length == 0 || installed.length != archive.length) return false;
        Set<String> a = signerHashes(installed), b = signerHashes(archive);
        return a.size() == installed.length && b.size() == archive.length && a.equals(b);
    }

    /** Probe the already verified APK through the exact URI sent to Android.
     * This checks access only; it does not replace full download or signer checks.
     */
    static void requireReadableApk(InputStream input, long reportedBytes, long expectedBytes) throws IOException {
        if (input == null || expectedBytes < 4 || expectedBytes > MAX_APK_BYTES
                || reportedBytes < -1 || reportedBytes >= 0 && reportedBytes != expectedBytes) {
            throw unreadableApk();
        }
        int[] magic = {0x50, 0x4b, 0x03, 0x04};
        for (int value : magic) if (input.read() != value) throw unreadableApk();
    }

    private static IOException unreadableApk() {
        return new IOException("无法读取更新安装包，请重新下载后重试。");
    }

    private static Set<String> signerHashes(byte[][] values) {
        Set<String> result = new HashSet<>();
        try {
            MessageDigest hash = MessageDigest.getInstance("SHA-256");
            for (byte[] item : values) {
                if (item == null || item.length == 0) return new HashSet<>();
                result.add(hex(hash.digest(item)));
            }
        } catch (java.security.NoSuchAlgorithmException impossible) { return new HashSet<>(); }
        return result;
    }

    static String hex(byte[] value) {
        char[] digits = "0123456789abcdef".toCharArray(), result = new char[value.length * 2];
        for (int n = 0; n < value.length; n++) { result[n * 2] = digits[(value[n] & 255) >>> 4]; result[n * 2 + 1] = digits[value[n] & 15]; }
        return new String(result);
    }

    private static String text(Map<String, Object> value, String name, int max) throws IOException {
        Object item = value.get(name);
        if (!(item instanceof String) || ((String) item).length() > max || !((String) item).equals(((String) item).trim())
                || ((String) item).isEmpty() || ((String) item).indexOf('\0') >= 0) throw invalid();
        return (String) item;
    }

    private static long integer(Map<String, Object> value, String name, long min, long max) throws IOException {
        Object item = value.get(name);
        if (!(item instanceof Integer || item instanceof Long)) throw invalid();
        long result = ((Number) item).longValue();
        if (result < min || result > max) throw invalid();
        return result;
    }

    private static IOException invalid() { return new IOException("更新信息格式不正确，请稍后重试。"); }
}

package cn.healthlife.local;

import android.app.Activity;
import android.content.ClipData;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.net.Uri;
import android.os.Build;
import android.os.ParcelFileDescriptor;
import android.os.StatFs;
import android.provider.Settings;
import androidx.core.content.FileProvider;
import org.json.JSONObject;
import org.json.JSONTokener;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;

/** User-initiated updates of this signed application; never reads member data. */
final class AppUpdater implements AutoCloseable {
    private final Activity activity;
    private final BooleanSupplier ownerActive;
    private final ExecutorService executor = Executors.newSingleThreadExecutor(r -> {
        Thread thread = new Thread(r, "fawnsee-app-update"); thread.setDaemon(true); return thread;
    });
    private final File directory;
    private final File trustedCache;
    private long generation;
    private String phase = "idle", errorMessage = "";
    private long downloaded;
    private ReleaseInfo release;
    private File part, verified;
    private boolean closed, installPending, installerHandedOff;
    private UpdateTransport transport;

    AppUpdater(Activity activity, BooleanSupplier ownerActive) {
        this.activity = activity;
        this.ownerActive = ownerActive;
        File cache = null;
        try { if (activity.getCacheDir() != null) cache=activity.getCacheDir().getCanonicalFile(); }
        catch (IOException ignored) { }
        this.trustedCache=cache;
        this.directory=cache == null ? null : new File(cache,"update");
        // Files already handed to Android may still be open. Only remove old
        // updater-owned leftovers; do not delete a newly handed-off package.
        File[] old = directory == null ? null : directory.listFiles();
        if (old != null) for (File file : old) {
            if (file.getName().matches("package-[0-9]+\\.(part|apk)")
                    && System.currentTimeMillis() - file.lastModified() > 24L * 60 * 60 * 1000) deleteOwned(file);
        }
    }

    synchronized JSONObject snapshot() {
        JSONObject value = new JSONObject();
        try {
            long total = release == null ? 0 : release.sizeBytes;
            int percent = "ready".equals(phase) || "permission_required".equals(phase) || "installer_opened".equals(phase)
                    ? 100 : total > 0 ? (int) Math.min(99, downloaded * 100 / total) : 0;
            value.put("phase",phase).put("current_version_code",BuildConfig.VERSION_CODE)
                    .put("current_version_name",BuildConfig.VERSION_NAME)
                    .put("version_name",release == null ? BuildConfig.VERSION_NAME : release.versionName)
                    .put("version_code",release == null ? BuildConfig.VERSION_CODE : release.versionCode)
                    .put("downloaded_bytes",downloaded).put("total_bytes",total).put("percent",percent)
                    .put("error_message",errorMessage)
                    .put("can_install",verified != null && verified.isFile() && hasInstallPermission());
        } catch (Exception ignored) { }
        return value;
    }

    synchronized JSONObject check() {
        if (closed || busy()) return snapshot();
        if (verified != null && verified.isFile() && release != null && release.versionCode > BuildConfig.VERSION_CODE) {
            phase = "ready"; errorMessage = ""; return snapshot();
        }
        final long token = ++generation;
        cleanupUnshared(); release = null; downloaded = 0; errorMessage = ""; phase = "checking";
        executor.execute(() -> checkRelease(token));
        return snapshot();
    }

    synchronized JSONObject start() {
        if (closed || busy()) return snapshot();
        if (!"available".equals(phase) || release == null || release.versionCode <= BuildConfig.VERSION_CODE) {
            return snapshot();
        }
        final long token = ++generation;
        final ReleaseInfo expected = release;
        phase = "downloading"; downloaded = 0; errorMessage = "";
        executor.execute(() -> download(token,expected));
        return snapshot();
    }

    JSONObject cancel() {
        UpdateTransport active;
        synchronized (this) {
            if (installPending || "installer_opened".equals(phase)) return snapshot();
            generation++;
            active = transport; transport = null;
            phase = "cancelled"; errorMessage = "";
            cleanupUnshared(); release = null; downloaded = 0;
        }
        if (active != null) active.close();
        return snapshot();
    }

    void install(Consumer<JSONObject> result) {
        final long token;
        final long expectedBytes;
        final File file;
        synchronized (this) {
            if (closed || installPending || verified == null || release == null
                    || !("ready".equals(phase) || "permission_required".equals(phase))
                    || !verified.isFile() || verified.length() != release.sizeBytes) {
                result.accept(snapshot()); return;
            }
            token = generation; file = verified; expectedBytes = release.sizeBytes; installPending = true;
            // Resolve and read on the updater worker, never on the UI thread.
            // The same content URI is then granted to the system installer.
            executor.execute(() -> prepareInstaller(token,file,expectedBytes,result));
        }
    }

    private void prepareInstaller(long token, File file, long expectedBytes, Consumer<JSONObject> result) {
        Uri readableUri = null;
        try {
            if (active(token)) {
                Uri candidate = FileProvider.getUriForFile(activity,BuildConfig.APPLICATION_ID+".updates",file);
                try (ParcelFileDescriptor descriptor = activity.getContentResolver().openFileDescriptor(candidate,"r")) {
                    if (descriptor == null) throw new IOException("无法读取更新安装包，请重新下载后重试。");
                    long reportedBytes = descriptor.getStatSize();
                    try (InputStream input = new ParcelFileDescriptor.AutoCloseInputStream(descriptor)) {
                        ReleaseInfo.requireReadableApk(input,reportedBytes,expectedBytes);
                    }
                }
                readableUri = candidate;
            }
        } catch (IOException | RuntimeException ignored) {
            // A generated URI alone does not prove that Android can open it.
        }
        final Uri uri = readableUri;
        activity.runOnUiThread(() -> {
            synchronized (AppUpdater.this) {
                try {
                    if (closed || token != generation || !ownerActive.getAsBoolean()) return;
                    if (uri == null) {
                        cleanupUnshared();
                        phase = "error";
                        errorMessage = "无法读取更新安装包，请重新下载后重试。";
                        return;
                    } else if (!hasInstallPermission()) {
                        Intent settings = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                Uri.parse("package:"+BuildConfig.APPLICATION_ID));
                        activity.startActivity(settings);
                        phase = "permission_required";
                    } else {
                        activity.startActivity(installerIntent(uri));
                        installerHandedOff = true; phase = "installer_opened";
                    }
                    errorMessage = "";
                } catch (RuntimeException ignored) {
                    phase = "error"; errorMessage = "无法打开系统安装界面，请稍后重试。";
                } finally {
                    installPending = false;
                    result.accept(snapshot());
                }
            }
        });
    }

    static Intent installerIntent(Uri uri) {
        Intent intent = new Intent(Intent.ACTION_VIEW).setDataAndType(uri,"application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        intent.setClipData(ClipData.newRawUri("FAWNSEE update",uri));
        return intent;
    }

    private void checkRelease(long token) {
        try (UpdateTransport network = connect(token)) {
            UpdateTransport.Reply reply = network.open(ReleaseInfo.MANIFEST_URL,-1,ReleaseInfo.MAX_MANIFEST_BYTES);
            byte[] raw = readManifest(reply.input,token);
            String text = StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(raw)).toString();
            JSONTokener parser = new JSONTokener(text);
            Object root = parser.nextValue();
            if (!(root instanceof JSONObject) || parser.nextClean() != 0) throw new IOException("更新信息格式不正确。");
            Map<String,Object> fields = new HashMap<>();
            JSONObject object = (JSONObject) root;
            for (Iterator<String> it=object.keys();it.hasNext();) { String key=it.next(); fields.put(key,object.get(key)); }
            ReleaseInfo candidate = ReleaseInfo.parse(fields,BuildConfig.APPLICATION_ID);
            synchronized (this) {
                if (!active(token)) return;
                release = candidate;
                if (candidate.versionCode <= BuildConfig.VERSION_CODE) phase = "up_to_date";
                else if (candidate.minSdk > Build.VERSION.SDK_INT) throw new IOException("此更新需要较新的安卓系统，当前版本仍可继续使用。");
                else phase = "available";
            }
        } catch (Exception error) { fail(token,error); }
        finally { clearTransport(token); }
    }

    private void download(long token, ReleaseInfo expected) {
        File temporary = null, complete = null;
        try (UpdateTransport network = connect(token)) {
            if (directory == null) throw new IOException("无法使用应用更新缓存，请重试。");
            if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("无法保存更新包，请检查可用空间。");
            if (!directory.getCanonicalFile().equals(directory.getAbsoluteFile())) throw new IOException("无法使用更新缓存目录。");
            if (new StatFs(directory.getPath()).getAvailableBytes() < expected.sizeBytes + 5L*1024*1024) throw new IOException("设备空间不足，请释放空间后重试。");
            // Generation is local to this owner; nanoTime avoids collisions with
            // a retained installer file from an older Activity instance.
            String key = Long.toString(System.nanoTime() & Long.MAX_VALUE);
            temporary = new File(directory,"package-"+key+".part");
            complete = new File(directory,"package-"+key+".apk");
            if (!temporary.createNewFile()) throw new IOException("无法创建更新缓存。");
            synchronized (this) { if (!active(token)) return; part = temporary; }
            UpdateTransport.Reply reply = network.open(expected.apkUrl,expected.sizeBytes,ReleaseInfo.MAX_APK_BYTES);
            try (FileOutputStream output = new FileOutputStream(temporary)) {
                ReleaseInfo.copyVerified(reply.input,output,expected.sizeBytes,expected.sha256,
                        () -> !active(token), count -> { synchronized (AppUpdater.this) { if (active(token)) downloaded=count; } });
                output.getFD().sync();
            }
            synchronized (this) {
                if (!active(token)) return;
                phase = "verifying";
                // PackageManager receives a conventional APK filename, but no
                // URI or ready state is published until its checks have passed.
                if (!temporary.renameTo(complete)) throw new IOException("无法保存待校验的更新包。");
                part=complete;
            }
            verifyArchive(complete,expected);
            synchronized (this) {
                if (!active(token)) return;
                part = null; verified = complete; installerHandedOff = false;
                phase = "ready"; downloaded = expected.sizeBytes;
                complete = null;
            }
        } catch (Exception error) { fail(token,error); }
        finally {
            if (temporary != null) deleteOwned(temporary);
            if (complete != null) deleteOwned(complete);
            clearTransport(token);
        }
    }

    private void verifyArchive(File file, ReleaseInfo expected) throws Exception {
        PackageManager manager = activity.getPackageManager();
        int flags = Build.VERSION.SDK_INT >= 28 ? PackageManager.GET_SIGNING_CERTIFICATES : PackageManager.GET_SIGNATURES;
        PackageInfo archive = manager.getPackageArchiveInfo(file.getPath(),flags);
        PackageInfo installed = manager.getPackageInfo(BuildConfig.APPLICATION_ID,flags);
        if (archive == null || archive.applicationInfo == null
                || !BuildConfig.APPLICATION_ID.equals(archive.packageName)
                || code(archive) != expected.versionCode || code(archive) <= code(installed)
                || !expected.versionName.equals(archive.versionName)
                || archive.applicationInfo.minSdkVersion != expected.minSdk
                || archive.applicationInfo.minSdkVersion > Build.VERSION.SDK_INT
                || (archive.applicationInfo.flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0
                || archive.splitNames != null && archive.splitNames.length != 0
                || !ReleaseInfo.sameSigners(signers(installed),signers(archive))) {
            throw new IOException("更新包身份或签名校验未通过，未启动安装。");
        }
    }

    private static long code(PackageInfo value) { return Build.VERSION.SDK_INT >= 28 ? value.getLongVersionCode() : value.versionCode; }
    private static byte[][] signers(PackageInfo value) {
        Signature[] signatures = Build.VERSION.SDK_INT >= 28
                ? value.signingInfo == null ? null : value.signingInfo.getApkContentsSigners() : value.signatures;
        if (signatures == null) return null;
        byte[][] result = new byte[signatures.length][];
        for (int n=0;n<signatures.length;n++) result[n]=signatures[n] == null ? null : signatures[n].toByteArray();
        return result;
    }

    private byte[] readManifest(InputStream input, long token) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        for (;;) {
            if (!active(token)) throw new IOException("更新已取消。");
            int count=input.read(buffer);
            if (count<0) break;
            if (count>ReleaseInfo.MAX_MANIFEST_BYTES-bytes.size()) throw new IOException("更新信息过大，请稍后重试。");
            bytes.write(buffer,0,count);
        }
        if (bytes.size()==0) throw new IOException("未能读取更新信息。");
        return bytes.toByteArray();
    }

    private synchronized UpdateTransport connect(long token) throws IOException {
        if (!active(token)) throw new IOException("更新已取消。");
        transport = new UpdateTransport(); return transport;
    }
    private synchronized boolean active(long token) { return !closed && token==generation && ownerActive.getAsBoolean(); }
    private synchronized void clearTransport(long token) { if (token==generation) transport=null; }
    private synchronized boolean busy() { return "checking".equals(phase)||"downloading".equals(phase)||"verifying".equals(phase)||installPending; }
    private boolean hasInstallPermission() { return Build.VERSION.SDK_INT < 26 || activity.getPackageManager().canRequestPackageInstalls(); }
    private synchronized void fail(long token, Exception error) {
        if (!active(token)) return;
        phase="error";
        errorMessage=error instanceof IOException && error.getMessage()!=null && error.getMessage().matches("[\\s\\S]*[\\p{IsHan}][\\s\\S]*")
                ? error.getMessage() : "更新未完成，请检查网络后重试。";
        cleanupUnshared();
    }
    private void cleanupUnshared() {
        if (part != null) deleteOwned(part);
        if (!installerHandedOff && verified != null) deleteOwned(verified);
        part=null; verified=null; installerHandedOff=false;
    }
    private void deleteOwned(File file) {
        try {
            if (directory != null && trustedCache != null && file.isFile()
                    && directory.getCanonicalFile().equals(new File(trustedCache,"update"))
                    && file.getCanonicalFile().getParentFile().equals(directory.getCanonicalFile())) file.delete();
        }
        catch (IOException ignored) { }
    }
    @Override public void close() {
        UpdateTransport active;
        synchronized (this) {
            if (closed) return;
            closed=true; generation++; active=transport; transport=null; cleanupUnshared();
        }
        if (active!=null) active.close();
        executor.shutdownNow();
    }
}

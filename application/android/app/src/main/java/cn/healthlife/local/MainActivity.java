package cn.healthlife.local;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.OpenableColumns;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.JsResult;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;
import androidx.webkit.WebViewAssetLoader;
import androidx.activity.ComponentActivity;
import androidx.activity.OnBackPressedCallback;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import org.json.JSONObject;
import org.vosk.LibVosk;
import org.vosk.LogLevel;
import org.vosk.Model;
import org.vosk.Recognizer;
import org.vosk.android.RecognitionListener;
import org.vosk.android.SpeechService;
import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Set;
import java.util.Arrays;
import java.util.HashSet;
import java.util.HashMap;
import java.util.Collections;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicLong;

/** Local UI only: no server, arbitrary navigation, exposed filesystem, or stored API keys. */
public class MainActivity extends ComponentActivity implements RecognitionListener {
    public static final String APP_URL = "https://appassets.androidplatform.net/assets/www/index.html";
    private static final int OPEN_FILE = 41, OPEN_BACKUP = 42, SAVE_DOCUMENT = 43, MIC_PERMISSION = 44;
    private static final Set<String> OFFICIAL_LINKS = Collections.unmodifiableSet(new HashSet<>(Arrays.asList(
        "https://ollama.com/settings/keys", "https://ollama.com/pricing",
        "https://platform.deepseek.com/api_keys", "https://platform.deepseek.com/usage",
        "https://api-docs.deepseek.com/zh-cn/")));
    private static final ExecutorService worker = Executors.newSingleThreadExecutor();
    private static final AtomicLong instances = new AtomicLong();
    private long instanceGeneration;
    private FilePreview preview;
    private WebView webView;
    private volatile PyObject api;
    private volatile boolean destroyed, pickerBusy;
    private JSONObject pendingRequest;
    private File pendingFile;
    private Model voiceModel;
    private SpeechService speech;
    private Recognizer voiceRecognizer;
    private JSONObject voiceRequest;
    private boolean voiceStarting;
    private int voiceGeneration;
    private final StringBuilder voiceText = new StringBuilder();

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        instanceGeneration = instances.incrementAndGet();
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override public void handleOnBackPressed() { handleBack(); }
        });
        if (Build.VERSION.SDK_INT >= 33) setRecentsScreenshotEnabled(false);
        try { initializeView(); }
        catch (RuntimeException error) {
            TextView fallback = new TextView(this);
            fallback.setPadding(32, 80, 32, 32);
            fallback.setText(R.string.webview_unavailable);
            setContentView(fallback);
            return;
        }
        worker.execute(() -> {
            if (!isCurrent()) return;
            try {
                if (!Python.isStarted()) Python.start(new AndroidPlatform(getApplicationContext()));
                api = Python.getInstance().getModule("mobile_api");
                JSONObject initialized = new JSONObject(api.callAttr("initialize", new File(getFilesDir(), "data").getAbsolutePath()).toString());
                if (!initialized.optBoolean("ok")) throw new IllegalStateException();
            } catch (Exception error) {
                api = null;
                // Never put user data or provider responses into Logcat.
                event("startupError", "本地数据库初始化失败，请保留应用数据并联系开发者检查。");
            }
        });
        webView.loadUrl(APP_URL);
    }

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    private void initializeView() {
        webView = new WebView(this);
        webView.setBackgroundColor(Color.rgb(245, 248, 245));
        if (Build.VERSION.SDK_INT >= 26) webView.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setSupportMultipleWindows(false);
        settings.setMediaPlaybackRequiresUserGesture(true);
        CookieManager.getInstance().setAcceptCookie(false);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, false);
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        WebViewAssetLoader loader = new WebViewAssetLoader.Builder()
            .addPathHandler("/assets/www/", path -> {
                // Only these immutable assets may reach the WebView; never Python or model files.
                if (!Arrays.asList("index.html", "app.css", "app.js").contains(path)) return denied();
                try {
                    String mime = path.endsWith(".js") ? "application/javascript" : path.endsWith(".css") ? "text/css" : "text/html";
                    WebResourceResponse response = new WebResourceResponse(mime, "UTF-8", getAssets().open("www/" + path));
                    Map<String, String> headers = new HashMap<>();
                    headers.put("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'");
                    headers.put("X-Content-Type-Options", "nosniff");
                    headers.put("Cache-Control", "no-store");
                    response.setResponseHeaders(headers);
                    return response;
                } catch (Exception ignored) { return denied(); }
            }).build();
        webView.setWebViewClient(new WebViewClient() {
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                WebResourceResponse result = loader.shouldInterceptRequest(request.getUrl());
                return result == null ? denied() : result;
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !APP_URL.equals(request.getUrl().toString());
            }
            @Override public void onReceivedSslError(WebView view, android.webkit.SslErrorHandler handler, android.net.http.SslError error) {
                handler.cancel();
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this).setMessage(message).setPositiveButton("知道了", (dialog, which) -> result.confirm())
                    .setOnCancelListener(dialog -> result.cancel()).show();
                return true;
            }
            @Override public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this).setTitle("请确认").setMessage(message)
                    .setPositiveButton("确定", (dialog, which) -> result.confirm()).setNegativeButton("取消", (dialog, which) -> result.cancel())
                    .setOnCancelListener(dialog -> result.cancel()).show();
                return true;
            }
        });
        webView.addJavascriptInterface(new Bridge(), "AndroidBridge");
        webView.setOnApplyWindowInsetsListener((view, insets) -> {
            int top, bottom, left, right;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                top = bars.top; bottom = bars.bottom; left = bars.left; right = bars.right;
            } else {
                top = insets.getSystemWindowInsetTop(); bottom = insets.getSystemWindowInsetBottom();
                left = insets.getSystemWindowInsetLeft(); right = insets.getSystemWindowInsetRight();
            }
            view.setPadding(left, top, right, bottom);
            return insets;
        });
        setContentView(webView);
        webView.requestApplyInsets();
    }

    private static WebResourceResponse denied() {
        return new WebResourceResponse("text/plain", "UTF-8", 403, "Forbidden", Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
    }

    public final class Bridge {
        @JavascriptInterface public void postMessage(String text) {
            if (destroyed || text == null || text.length() > 400_000) return;
            worker.execute(() -> {
                if (!isCurrent()) return;
                JSONObject request;
                try { request = new JSONObject(text); }
                catch (Exception ignored) { return; }
                if (pickerBusy) { failure(request, "请先完成或取消当前文件选择操作"); return; }
                try {
                    if (api == null) { failure(request, "本地服务尚未就绪，请关闭后重新打开应用"); return; }
                    String action = request.getString("action");
                    if (action.startsWith("native.")) nativeAction(request);
                    else {
                        if (action.equals("auth.logout")) runOnUiThread(() -> stopVoice());
                        JSONObject response = new JSONObject(api.callAttr("dispatch", request.toString()).toString());
                        deliver(response);
                    }
                } catch (Exception ignored) { failure(request, "操作未完成，请检查输入或稍后重试；已有资料会保留"); }
            });
        }
    }

    private void nativeAction(JSONObject request) throws Exception {
        JSONObject params = request.optJSONObject("params");
        if (params == null) params = new JSONObject();
        String action = request.getString("action");
        switch (action) {
            case "native.getInfo":
                success(request, new JSONObject().put("platform", "Android").put("version", BuildConfig.VERSION_NAME)
                    .put("android", Build.VERSION.RELEASE).put("timezone", "北京时间 UTC+8").put("offline_ocr", true).put("offline_voice", true));
                break;
            case "native.openUrl": {
                String url = params.getString("url");
                if (!OFFICIAL_LINKS.contains(url)) { failure(request, "只能打开列出的官方密钥、额度或说明页面"); return; }
                runOnUiThread(() -> {
                    if (!isCurrent()) return;
                    try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url))); success(request, JSONObject.NULL); }
                    catch (RuntimeException ignored) { failure(request, "手机没有可打开官网的浏览器"); }
                });
                break;
            }
            case "native.importFile":
                if (!isMember()) { failure(request, "请先登录会员账号"); return; }
                beginPicker(request, new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("*/*")
                    .putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"image/jpeg", "image/png", "image/webp", "application/pdf"}), OPEN_FILE);
                break;
            case "native.importBackup": {
                if (!params.optBoolean("confirm")) { failure(request, "导入将替换本机数据，请先确认并做好备份"); return; }
                if (!state().optBoolean("can_import")) { failure(request, "当前账号无权导入完整数据库"); return; }
                beginPicker(request, new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("*/*"), OPEN_BACKUP);
                break;
            }
            case "native.exportBackup": {
                if (!state().optBoolean("can_backup")) { failure(request, "当前账号无权导出完整数据库"); return; }
                File staged = tempFile("backup", ".zip");
                // Backup service requires an unused destination.
                if (!staged.delete()) throw new IllegalStateException();
                JSONObject result = nativeResult("backup_export", staged.getAbsolutePath());
                if (!result.optBoolean("ok")) { cleanup(staged); forward(request, result); return; }
                pendingFile = staged;
                beginPicker(request, new Intent(Intent.ACTION_CREATE_DOCUMENT).setType("application/zip")
                    .putExtra(Intent.EXTRA_TITLE, "健康生活数据备份.zip"), SAVE_DOCUMENT);
                break;
            }
            case "native.exportFile": {
                File staged = tempFile("attachment", ".bin");
                JSONObject result = nativeResult("attachment_export", params.getInt("file_id"), staged.getAbsolutePath());
                if (!result.optBoolean("ok")) { cleanup(staged); forward(request, result); return; }
                JSONObject metadata = result.getJSONObject("result");
                pendingFile = staged;
                beginPicker(request, new Intent(Intent.ACTION_CREATE_DOCUMENT).setType(metadata.optString("media_type", "application/octet-stream"))
                    .putExtra(Intent.EXTRA_TITLE, metadata.optString("original_name", "附件")), SAVE_DOCUMENT);
                break;
            }
            case "native.ocrFile": {
                JSONObject pythonRequest = new JSONObject().put("id", request.opt("id")).put("action", "files.extract_text")
                    .put("params", new JSONObject().put("file_id", params.getInt("file_id")));
                deliver(new JSONObject(api.callAttr("dispatch", pythonRequest.toString()).toString()));
                break;
            }
            case "native.previewFile": {
                File staged = tempFile("preview", ".bin");
                JSONObject result = nativeResult("attachment_export", params.getInt("file_id"), staged.getAbsolutePath());
                if (!result.optBoolean("ok")) { cleanup(staged); forward(request, result); return; }
                JSONObject metadata = result.getJSONObject("result");
                runOnUiThread(() -> {
                    if (destroyed) { cleanup(staged); return; }
                    if (preview != null) preview.close();
                    preview = FilePreview.show(this, staged, metadata.optString("media_type", ""), metadata.optString("original_name", "附件"));
                    success(request, new JSONObject());
                });
                break;
            }
            case "native.voiceStart":
                if (!isMember()) { failure(request, "请先登录会员账号"); return; }
                runOnUiThread(() -> startVoice(request));
                break;
            case "native.voiceStop":
                runOnUiThread(() -> { stopVoice(); success(request, new JSONObject()); });
                break;
            default: failure(request, "不支持的手机操作");
        }
    }

    private JSONObject state() throws Exception {
        return new JSONObject(api.callAttr("dispatch", "{\"id\":0,\"action\":\"auth.state\",\"params\":{}}").toString()).getJSONObject("result");
    }
    private boolean isMember() throws Exception {
        JSONObject user = state().optJSONObject("user");
        return user != null && "member".equals(user.optString("role_code"));
    }
    private JSONObject nativeResult(String method, Object... args) throws Exception {
        return new JSONObject(api.callAttr(method, args).toString());
    }

    private void beginPicker(JSONObject request, Intent intent, int code) {
        pickerBusy = true;
        pendingRequest = request;
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        runOnUiThread(() -> {
            if (!isCurrent()) { pickerBusy = false; cleanup(pendingFile); pendingFile = null; return; }
            try { startActivityForResult(intent, code); }
            catch (RuntimeException error) { pickerBusy = false; pendingRequest = null; cleanup(pendingFile); pendingFile = null; failure(request, "无法打开系统文件选择器"); }
        });
    }

    @Override protected void onActivityResult(int code, int resultCode, Intent intent) {
        super.onActivityResult(code, resultCode, intent);
        if (code != OPEN_FILE && code != OPEN_BACKUP && code != SAVE_DOCUMENT) return;
        JSONObject request = pendingRequest;
        File staged = pendingFile;
        pendingRequest = null;
        pendingFile = null;
        if (request == null) {
            pickerBusy = false; cleanup(staged);
            new AlertDialog.Builder(this).setMessage("文件操作因应用重启而中断，请重新登录后再操作。若刚才选择了导出位置，请核对文件是否为空，必要时重新导出。")
                .setPositiveButton("知道了", null).show();
            return;
        }
        if (resultCode != RESULT_OK || intent == null || intent.getData() == null) {
            pickerBusy = false; cleanup(staged); failure(request, "已取消，原有数据未更改"); return;
        }
        Uri uri = intent.getData();
        worker.execute(() -> {
            if (!isCurrent()) { cleanup(staged); pickerBusy = false; return; }
            File inputFile = null;
            try {
                if (!"content".equals(uri.getScheme())) throw new IllegalArgumentException();
                if (code == SAVE_DOCUMENT) {
                    try (InputStream source = new FileInputStream(staged); OutputStream target = getContentResolver().openOutputStream(uri, "wt")) {
                        if (target == null) throw new IllegalArgumentException();
                        transfer(source, target, 2L * 1024 * 1024 * 1024);
                    }
                    success(request, new JSONObject().put("message", "已保存到你选择的位置"));
                } else {
                    String name = displayName(uri);
                    inputFile = tempFile("picked", code == OPEN_BACKUP ? ".zip" : ".bin");
                    try (InputStream source = getContentResolver().openInputStream(uri); OutputStream target = new FileOutputStream(inputFile)) {
                        if (source == null) throw new IllegalArgumentException();
                        transfer(source, target, code == OPEN_BACKUP ? 2L * 1024 * 1024 * 1024 : 15L * 1024 * 1024);
                    }
                    JSONObject response = code == OPEN_BACKUP ? nativeResult("backup_import", inputFile.getAbsolutePath())
                        : nativeResult("import_attachment", inputFile.getAbsolutePath(), name);
                    forward(request, response);
                }
            } catch (Exception error) { failure(request, "文件操作失败：请检查可用空间、文件大小和格式，原有数据库不会被直接删除"); }
            finally { cleanup(inputFile); cleanup(staged); pickerBusy = false; }
        });
    }

    private String displayName(Uri uri) {
        try (Cursor cursor = getContentResolver().query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) return cursor.getString(0);
        } catch (RuntimeException ignored) { }
        return "未命名文件";
    }
    private File tempFile(String prefix, String suffix) throws Exception {
        return File.createTempFile(prefix, suffix, getCacheDir());
    }
    private static void transfer(InputStream source, OutputStream target, long limit) throws Exception {
        byte[] buffer = new byte[64 * 1024];
        long total = 0;
        int read;
        while ((read = source.read(buffer)) != -1) {
            total += read;
            if (total > limit) throw new IllegalArgumentException("文件过大");
            target.write(buffer, 0, read);
        }
        target.flush();
    }
    private static void cleanup(File file) { if (file != null && file.isFile()) file.delete(); }

    private void startVoice(JSONObject request) {
        if (!isCurrent()) return;
        if (speech != null || voiceStarting) { failure(request, "正在录音或准备离线语音，请先停止"); return; }
        voiceRequest = request;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MIC_PERMISSION);
            return;
        }
        voiceStarting = true;
        final int generation = ++voiceGeneration;
        worker.execute(() -> {
            try {
                if (voiceModel == null) {
                    LibVosk.setLogLevel(LogLevel.WARNINGS);
                    File target = new File(getFilesDir(), "voice-model-cn-0.22");
                    File marker = new File(target, ".complete");
                    if (!marker.exists()) {
                        copyAssetTree("vosk-model-small-cn-0.22", target);
                        if (!marker.createNewFile()) throw new IllegalStateException();
                    }
                    voiceModel = new Model(target.getAbsolutePath());
                }
                runOnUiThread(() -> {
                    if (generation != voiceGeneration) { failure(request, "录音准备已取消"); return; }
                    voiceStarting = false;
                    if (destroyed || !hasWindowFocus()) { failure(request, "已停止准备录音，请返回应用再试"); return; }
                    try {
                        voiceText.setLength(0);
                        voiceRecognizer = new Recognizer(voiceModel, 16000.0f);
                        speech = new SpeechService(voiceRecognizer, 16000.0f);
                        speech.startListening(new RecognitionListener() {
                            public void onResult(String text) { if (generation == voiceGeneration) MainActivity.this.onResult(text); }
                            public void onPartialResult(String text) { if (generation == voiceGeneration) MainActivity.this.onPartialResult(text); }
                            public void onFinalResult(String text) { if (generation == voiceGeneration) MainActivity.this.onFinalResult(text); }
                            public void onError(Exception error) { if (generation == voiceGeneration) MainActivity.this.onError(error); }
                            public void onTimeout() { if (generation == voiceGeneration) MainActivity.this.onTimeout(); }
                        }, 120000);
                        success(request, new JSONObject().put("recording", true));
                        event("voiceStarted", "离线录音中，最多两分钟；不会自动发送识别结果");
                    } catch (Exception error) { stopVoice(); failure(request, "无法启动麦克风，请检查权限或其他应用占用"); }
                });
            } catch (Exception error) {
                runOnUiThread(() -> { if (generation == voiceGeneration) voiceStarting = false; failure(request, "离线语音初始化失败，请检查手机可用空间后重试"); });
            }
        });
    }

    private void copyAssetTree(String source, File target) throws Exception {
        String[] children = getAssets().list(source);
        if (children != null && children.length > 0) {
            if (!target.isDirectory() && !target.mkdirs()) throw new IllegalStateException();
            for (String child : children) copyAssetTree(source + "/" + child, new File(target, child));
        } else {
            try (InputStream input = getAssets().open(source); OutputStream output = new FileOutputStream(target)) {
                transfer(input, output, 100L * 1024 * 1024);
            }
        }
    }
    private void stopVoice() {
        voiceGeneration++;
        if (voiceStarting && voiceRequest != null) failure(voiceRequest, "录音准备已取消");
        voiceStarting = false;
        // cancel suppresses Vosk's queued final callback; flush synchronously after join
        // so a short utterance is not lost and cannot arrive in a newer recording.
        if (speech != null) { speech.cancel(); speech.shutdown(); speech = null;
            if (voiceRecognizer != null) { try { appendVoice(voiceRecognizer.getFinalResult()); } catch (RuntimeException ignored) { } }
        }
        if (voiceRecognizer != null) { voiceRecognizer.close(); voiceRecognizer = null; }
        event("voiceStopped", voiceText.toString());
    }
    @Override public void onRequestPermissionsResult(int code, String[] permissions, int[] grants) {
        super.onRequestPermissionsResult(code, permissions, grants);
        if (code == MIC_PERMISSION && voiceRequest != null) {
            if (grants.length > 0 && grants[0] == PackageManager.PERMISSION_GRANTED) startVoice(voiceRequest);
            else failure(voiceRequest, "未授予麦克风权限，仍可正常输入文字");
        }
    }
    @Override public void onResult(String hypothesis) { appendVoice(hypothesis); }
    @Override public void onFinalResult(String hypothesis) { appendVoice(hypothesis); stopVoice(); }
    private void appendVoice(String hypothesis) {
        try {
            String value = new JSONObject(hypothesis).optString("text").replaceAll("(?<=[\\p{IsHan}]) +(?=[\\p{IsHan}])", "");
            if (!value.trim().isEmpty()) { voiceText.append(value); event("voiceResult", voiceText.toString()); }
        } catch (Exception ignored) { }
    }
    @Override public void onPartialResult(String hypothesis) {
        try { event("voicePartial", voiceText + new JSONObject(hypothesis).optString("partial")); }
        catch (Exception ignored) { }
    }
    @Override public void onError(Exception error) { stopVoice(); event("voiceError", "录音已停止，可继续输入文字"); }
    @Override public void onTimeout() { stopVoice(); }

    private void success(JSONObject request, Object result) {
        try { deliver(new JSONObject().put("id", request.opt("id")).put("ok", true).put("result", result)); }
        catch (Exception ignored) { }
    }
    private void failure(JSONObject request, String message) {
        try { deliver(new JSONObject().put("id", request.opt("id")).put("ok", false).put("error", message)); }
        catch (Exception ignored) { }
    }
    private void forward(JSONObject request, JSONObject response) throws Exception {
        response.put("id", request.opt("id"));
        deliver(response);
    }
    private void deliver(JSONObject response) {
        runOnUiThread(() -> {
            if (isCurrent() && webView != null) webView.evaluateJavascript("window.onNativeResult&&window.onNativeResult(JSON.parse(" + JSONObject.quote(response.toString()) + "))", null);
        });
    }
    private void event(String type, String text) {
        try {
            String encoded = JSONObject.quote(new JSONObject().put("type", type).put("text", text).toString());
            runOnUiThread(() -> { if (isCurrent() && webView != null) webView.evaluateJavascript("window.onNativeEvent&&window.onNativeEvent(JSON.parse(" + encoded + "))", null); });
        } catch (Exception ignored) { }
    }
    private void handleBack() {
        if (webView != null) webView.evaluateJavascript("window.onAndroidBack?window.onAndroidBack():false", handled -> {
            if (!"true".equals(handled)) new AlertDialog.Builder(this).setMessage("退出应用？请先保存正在填写的内容。").setPositiveButton("退出", (d, w) -> finish()).setNegativeButton("取消", null).show();
        }); else finish();
    }
    @Override protected void onPause() { super.onPause(); stopVoice(); }
    private boolean isCurrent() { return !destroyed && instanceGeneration == instances.get(); }
    @Override protected void onDestroy() {
        destroyed = true;
        stopVoice();
        cleanup(pendingFile);
        if (preview != null) { preview.close(); preview = null; }
        worker.execute(() -> {
            try {
                if (api != null && instanceGeneration == instances.get()) api.callAttr("dispatch", "{\"id\":0,\"action\":\"auth.logout\",\"params\":{}}");
            } finally { if (voiceModel != null) { voiceModel.close(); voiceModel = null; } }
        });
        if (webView != null) { webView.removeJavascriptInterface("AndroidBridge"); webView.destroy(); }
        super.onDestroy();
    }
}

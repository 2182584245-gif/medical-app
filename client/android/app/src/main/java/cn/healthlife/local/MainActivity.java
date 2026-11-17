package cn.healthlife.local;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.OpenableColumns;
import android.provider.MediaStore;
import android.provider.Settings;
import android.util.Base64;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.JsResult;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;
import android.widget.FrameLayout;
import android.view.ViewGroup;
import androidx.activity.ComponentActivity;
import androidx.activity.OnBackPressedCallback;
import androidx.core.content.FileProvider;
import androidx.core.content.ContextCompat;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.atomic.AtomicLong;

/** An immutable packaged design surface; HTTPS business traffic stays in the Python member facade. */
public class MainActivity extends ComponentActivity {
    public static final String APP_URL = PackagedResources.ORIGIN + PackagedResources.PREFIX + "index.html";
    static final int IMAGE_LIMIT = 2 * 1024 * 1024;
    private static final int PICK_IMAGES = 41, MIC_PERMISSION = 44, CAPTURE_IMAGE = 45, PICK_FILES = 46;
    private static final int DOCUMENT_LIMIT = 5 * 1024 * 1024;
    // A protected 16 MiB attachment draft needs about 22 MiB as base64.
    private static final int MAX_REQUEST_CHARS = 24 * 1024 * 1024;
    private static final ExecutorService worker = Executors.newSingleThreadExecutor();
    private static final ExecutorService readWorker = NativeReadLanePolicy.executor();
    private static final ExecutorService onlineReadWorker = NativeOnlineReadLanePolicy.executor();
    private static final ExecutorService uploadWorker = NativeUploadLanePolicy.executor();
    private static final ExecutorService reminderWorker = NativeReminderLanePolicy.executor();
    private static final ExecutorService aiWorker = Executors.newFixedThreadPool(2);
    private static final ExecutorService chatControlWorker = Executors.newSingleThreadExecutor();
    private static final AtomicLong instances = new AtomicLong();
    private long instanceGeneration;
    private WebView webView;
    private final WebPageSessions pageSessions = new WebPageSessions();
    private PackagedResources resources;
    private AppUpdater appUpdater;
    private volatile PyObject api;
    private volatile boolean destroyed, pickerBusy, trustedPage;
    private boolean appResumed;
    private JSONObject pendingPicker;
    private File pendingCameraFile;
    private Uri pendingCameraUri;
    private int pendingImageCount = 1, pendingOwner, pendingPickerCode, pendingPickerKind, pickerSequence=1000;
    private String pendingScopeId="",pendingScopeEpoch="",pendingPrivateRoot="";
    private final ExecutorService voiceWorker = Executors.newSingleThreadExecutor();
    // Local media operations never queue behind a network/business RPC.
    private final ExecutorService mediaWorker = Executors.newSingleThreadExecutor();
    private static final int APP_PERMISSIONS = 14942;
    private static final int CAMERA_PERMISSION = 14943;
    private JSONObject pendingPermissionRequest;
    private volatile boolean entryPermissionsPending, specialSettingsPending;
    private boolean remindersPrepared;
    private boolean notificationDeniedAtEntry,notificationSettingsAttemptedAtEntry,notificationRuntimePending;
    private Intent cameraAfterPermission;
    private final AtomicLong voiceAuthEpoch = new AtomicLong();
    private NativeVoiceCapture voiceCapture;
    private JSONObject voiceRequest;
    private final List<JSONObject> voiceStopRequests = new ArrayList<>();
    private boolean voiceStarting, voiceProcessing, awaitingMicPermission;
    private File voiceInFlightWav;
    private volatile int voiceGeneration;
    private int voiceOwner;
    private String voiceScopeId="",voiceScopeEpoch="",voicePrivateRoot="";
    private long voiceAccountEpoch;
    private String voiceText = "";
    private volatile String activeChatOperation = "", cancelledChatOperation = "";
    private JSONObject voiceCompleted;
    private final Runnable guestInvalidator=()-> {
        if(!isCurrent())return;
        voiceAuthEpoch.incrementAndGet();cancelVoice();cleanupCamera();pendingPicker=null;pickerBusy=false;pendingPickerCode=0;
        String operation; synchronized(MainActivity.this){operation=activeChatOperation;activeChatOperation="";}
        if(!operation.isEmpty()) {
            cancelledChatOperation=operation;
            chatControlWorker.execute(()->{try{if(api!=null)api.callAttr("cancel_chat",operation);}catch(Exception ignored){}});
        }
    };
    private boolean localReceiverRegistered;
    private final BroadcastReceiver localReceiver=new BroadcastReceiver() {
        @Override public void onReceive(Context context,Intent intent) {
            if(!isCurrent() || !trustedPage || intent==null)return;
            if(LocalReminders149.CHANGED_ACTION.equals(intent.getAction())) {
                remindersPrepared=true;
                if(webView!=null)webView.post(MainActivity.this::requestEntryPermissions);
                return;
            }
            String status=intent.getStringExtra("status");
            if(status==null || status.length()>64000)return;
            try {event("localSync149",new JSONObject(status));}catch(Exception ignored) { }
        }
    };

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        instanceGeneration = instances.incrementAndGet();
        TextView preparing=new TextView(this);preparing.setPadding(32,80,32,32);
        preparing.setText("正在准备本机数据…");setContentView(preparing);
        ClientDataReset.prepare(this,error-> {
            if(!isCurrent())return;
            if(error!=null) {preparing.setText("本机数据清理尚未完成，请重新打开应用。旧账号暂不可使用。");return;}
            initializeAfterReset();
        });
    }

    private void initializeAfterReset() {
        NativeLocalHooks149.installGuestInvalidator(guestInvalidator);
        voiceWorker.execute(() -> {
            try { VoiceWav.cleanAbandoned(new File(new File(getFilesDir(), "member"), "voice-capture")); }
            catch (Exception ignored) { } // A bad directory is rejected again before capture, without deleting elsewhere.
            try { RetiredVoiceModel.cleanup(getFilesDir()); }
            catch (Exception ignored) { } // Only the exact private retired-model path is eligible.
        });
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override public void handleOnBackPressed() { handleBack(); }
        });
        if (Build.VERSION.SDK_INT >= 33) setRecentsScreenshotEnabled(false);
        try { initializeView(); }
        catch (Exception error) {
            TextView fallback = new TextView(this);
            fallback.setPadding(32, 80, 32, 32);
            fallback.setText(R.string.webview_unavailable);
            setContentView(fallback);
            return;
        }
        appUpdater = new AppUpdater(this, this::isCurrent);
        IntentFilter localEvents=new IntentFilter(BuildConfig.APPLICATION_ID+".LOCAL_SYNC149");
        localEvents.addAction(LocalReminders149.CHANGED_ACTION);
        ContextCompat.registerReceiver(this,localReceiver,localEvents,ContextCompat.RECEIVER_NOT_EXPORTED);
        localReceiverRegistered=true;
        worker.execute(() -> {
            if (!isCurrent()) return;
            try {
                api = MobileRuntime149.module(getApplicationContext());
                JSONObject scope=nativeScope();
                if("guest".equals(scope.optString("kind")))VoiceWav.cleanAbandoned(new File(scope.getString("private_root"),"voice-capture"));
                api.callAttr("on_foreground");
                event("nativeReady", new JSONObject());
            } catch (Exception error) {
                api = null;
                eventText("startupError", "会员服务初始化未完成，请重新打开应用。");
            }
        });
        webView.loadUrl(APP_URL);
        webView.post(this::requestEntryPermissions);
    }

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    private void initializeView() throws Exception {
        pageSessions.open();
        resources = new PackagedResources(getAssets());
        webView = new WebView(this);
        webView.setBackgroundColor(Color.rgb(245, 240, 228));
        if (Build.VERSION.SDK_INT >= 26) webView.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
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
        webView.setWebViewClient(new WebViewClient() {
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return resources.load(request.getUrl());
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (request.isForMainFrame()) return !isTrustedEntry(uri);
                return !(resources.isAllowed(uri) || "about:blank".equals(uri.toString()) || "about:srcdoc".equals(uri.toString()));
            }
            @Override public void onPageStarted(WebView view, String url, Bitmap favicon) {
                trustedPage = isTrustedEntry(Uri.parse(url));
                if (!trustedPage) view.stopLoading();
            }
            @Override public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                if (view == webView && url != null && isTrustedEntry(Uri.parse(url))) publishAppVisibility();
            }
            @Override public void onReceivedSslError(WebView view, android.webkit.SslErrorHandler handler, android.net.http.SslError error) {
                handler.cancel();
            }
            @android.annotation.TargetApi(26)
            @Override public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                handleRendererExit(view, detail.didCrash());
                return true;
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this).setMessage(message).setPositiveButton("知道了", (dialog, which) -> result.confirm())
                    .setOnCancelListener(dialog -> result.cancel()).show();
                return true;
            }
            @Override public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this).setMessage(message)
                    .setPositiveButton("确定", (dialog, which) -> result.confirm()).setNegativeButton("取消", (dialog, which) -> result.cancel())
                    .setOnCancelListener(dialog -> result.cancel()).show();
                return true;
            }
        });
        webView.addJavascriptInterface(new Bridge(), "AndroidBridge");
        FrameLayout host = new FrameLayout(this);
        host.setBackgroundColor(Color.rgb(253, 251, 247));
        host.addView(webView, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        // WebView padding does not reliably inset fixed/srcdoc content. Inset its native host instead.
        host.setOnApplyWindowInsetsListener((view, insets) -> {
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
        setContentView(host);
        host.requestApplyInsets();
    }

    /** A dead renderer cannot be reused. Recovery is explicit and never replays a request. */
    private void handleRendererExit(WebView view, boolean crashed) {
        android.util.Log.w("FawnseeVoice", crashed ? "web_renderer_crashed" : "web_renderer_terminated");
        boolean active = view == webView;
        if (active) {
            // Detach before cancellation: its completion events must never reach a dead WebView.
            pageSessions.open();
            trustedPage = false;
            webView = null;
            cancelVoice();
        }
        if (view.getParent() instanceof ViewGroup) ((ViewGroup) view.getParent()).removeView(view);
        try { view.destroy(); }
        catch (RuntimeException | LinkageError error) {
            android.util.Log.w("FawnseeVoice", "web_dispose:" + error.getClass().getSimpleName());
        }
        if (active && isCurrent()) showPageRecovery();
    }

    private void showPageRecovery() {
        android.widget.LinearLayout panel = new android.widget.LinearLayout(this);
        panel.setOrientation(android.widget.LinearLayout.VERTICAL);
        panel.setGravity(android.view.Gravity.CENTER);
        int padding = (int) (28 * getResources().getDisplayMetrics().density);
        panel.setPadding(padding, padding, padding, padding);
        panel.setBackgroundColor(Color.rgb(253, 251, 247));
        TextView message = new TextView(this);
        message.setText("页面暂时无法显示，请重新打开页面。录音已停止。已保存的本机资料不受影响。");
        message.setTextSize(17);
        message.setTextColor(Color.rgb(32, 68, 58));
        panel.addView(message);
        android.widget.Button reopen = new android.widget.Button(this);
        reopen.setText("重新打开页面");
        reopen.setTextColor(Color.WHITE);
        reopen.setBackgroundTintList(android.content.res.ColorStateList.valueOf(Color.rgb(30, 111, 108)));
        reopen.setOnClickListener(view -> {
            if (!isCurrent() || webView != null) return;
            try { initializeView(); webView.loadUrl(APP_URL); }
            catch (Exception | LinkageError error) {
                android.util.Log.w("FawnseeVoice", "web_reopen:" + error.getClass().getSimpleName());
                WebView failed = webView;
                trustedPage = false; webView = null;
                if (failed != null) {
                    if (failed.getParent() instanceof ViewGroup) ((ViewGroup) failed.getParent()).removeView(failed);
                    try { failed.destroy(); } catch (RuntimeException | LinkageError ignored) { }
                }
                showPageRecovery();
            }
        });
        panel.addView(reopen, new android.widget.LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        setContentView(panel);
    }

    static boolean isTrustedEntry(Uri uri) {
        return uri != null && "https".equals(uri.getScheme()) && "appassets.androidplatform.net".equals(uri.getHost())
            && uri.getPort() == -1 && uri.getUserInfo() == null && uri.getEncodedQuery() == null
            && (PackagedResources.PREFIX + "index.html").equals(uri.getEncodedPath());
    }

    public final class Bridge {
        private final long pageEpoch = pageSessions.current();
        @JavascriptInterface public void postMessage(String text) {
            if (!isCurrent() || !trustedPage || !pageSessions.accepts(pageEpoch)
                    || text == null || text.length() > MAX_REQUEST_CHARS) return;
            final JSONObject request;
            try {
                request = new JSONObject(text);
                Object id = request.get("id");
                if (!(id instanceof String || id instanceof Number) || id.toString().length() > 100
                        || request.getString("action").length() > 120 || !(request.opt("params") instanceof JSONObject)) return;
            } catch (Exception ignored) { return; }
            if (!pageSessions.accepts(pageEpoch)) return;
            pageSessions.bind(request, pageEpoch);
            String action = request.optString("action");
            final long requestAccountEpoch = voiceAuthEpoch.get();
            final JSONObject businessParams = request.optJSONObject("params");
            final boolean chatRequest = ("member.request".equals(action)||"guest.request".equals(action)) && businessParams != null
                && "chat.send".equals(businessParams.optString("action"));
            final String chatOperation = chatRequest ? businessParams.optString("request_id", "") : "";
            if (chatRequest) {
                if (!chatOperation.matches("[0-9a-fA-F-]{36}")) { failure(request,"invalid_request","对话请求格式不正确。");return; }
                synchronized (MainActivity.this) {
                    if (!activeChatOperation.isEmpty()) { failure(request,"chat_busy","请等待当前回答完成，或先停止回答。");return; }
                    activeChatOperation = chatOperation;
                }
            }
            if (action.equals("native.cancelChat")) {
                try {
                    JSONObject params = request.getJSONObject("params");
                    requireFields(params, "operation_id");
                    final String operation = params.getString("operation_id");
                    if (!operation.matches("[0-9a-fA-F-]{36}")) throw new IllegalArgumentException();
                    if (!operation.equals(activeChatOperation)) {
                        success(request,new JSONObject().put("requested",false).put("cancelled",false));return;
                    }
                    cancelledChatOperation = operation;
                    chatControlWorker.execute(() -> {
                        try {
                            PyObject currentApi = api;
                            JSONObject result = currentApi == null ? new JSONObject()
                                : new JSONObject(currentApi.callAttr("cancel_chat",operation).toString());
                            JSONObject body = result.optJSONObject("result");
                            success(request,new JSONObject().put("requested",true)
                                .put("cancelled",body != null && body.optBoolean("cancelled",false)));
                        } catch (Exception ignored) {
                            failure(request,"cancel_failed","暂未确认停止，请稍后重试。");
                        }
                    });
                } catch (Exception ignored) { failure(request,"invalid_request","停止回答请求格式不正确。"); }
                return;
            }
            if (action.equals("native.openLink")) {
                try {
                    JSONObject params = request.getJSONObject("params");
                    requireFields(params, "url");
                    String url = ReadingLinkPolicy.externalUrl(params.getString("url"));
                    runOnPageUi(request, () -> {
                        if (!isCurrent() || !trustedPage || requestAccountEpoch != voiceAuthEpoch.get()) {
                            failure(request, "account_changed", "页面已变化，请重新打开链接。"); return;
                        }
                        try {
                            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)).addCategory(Intent.CATEGORY_BROWSABLE));
                            success(request, JSONObject.NULL);
                        } catch (RuntimeException ignored) {
                            failure(request, "browser_unavailable", "无法打开浏览器，可复制链接后查看。");
                        }
                    });
                } catch (Exception ignored) { failure(request, "invalid_link", "原文链接格式无效。"); }
                return;
            }
            if(action.startsWith("native.")) {
                try {
                    JSONObject scope=nativeScope();
                    if(!("member".equals(scope.optString("kind")) && scope.optInt("actor_id",0)>0) && !GuestScopePolicy11614.allowedNative(action)) {
                        failure(request,"login_required","此功能需要登录后使用。");return;
                    }
                }catch(Exception ignored){}
            }
            // Updates have their own executor and never queue behind member RPC.
            if (action.startsWith("native.update.")) {
                try {
                    requireFields(request.getJSONObject("params"));
                    if (appUpdater == null) { failure(request,"not_ready","更新服务尚未就绪。"); return; }
                    switch (action) {
                        case "native.update.check": success(request,appUpdater.check()); break;
                        case "native.update.start": success(request,appUpdater.start()); break;
                        case "native.update.status": success(request,appUpdater.snapshot()); break;
                        case "native.update.cancel": success(request,appUpdater.cancel()); break;
                        case "native.update.install": appUpdater.install(value -> success(request,value)); break;
                        default: failure(request,"unsupported_action","不支持此更新操作。");
                    }
                } catch (Exception ignored) { failure(request,"invalid_request","更新请求格式不正确。"); }
                return;
            }
            // Stop/cancel must not wait behind a slow cloud request on the business worker.
            if (action.equals("native.stopVoice") || action.equals("native.cancelVoice")) {
                runOnPageUi(request, () -> stopVoiceRequest(request, action.equals("native.cancelVoice")));
                return;
            }
            if (action.equals("native.startVoice")) {
                // Reserve the session before member RPC queues, so an early release can always cancel it.
                runOnPageUi(request, () -> requestVoiceStart(request));
                return;
            }
            boolean guestAuthAttempt=false;
            try{guestAuthAttempt=!action.equals("auth.logout") && "guest".equals(nativeScope().optString("kind"));}catch(Exception ignored){}
            if (!guestAuthAttempt && (action.equals("auth.logout") || action.equals("auth.login") || action.equals("auth.register")
                    || action.equals("auth.register.complete") || action.equals("auth.register.verified")
                    || action.equals("auth.phone.login") || action.equals("auth.phone.entry") || action.equals("auth.phone.quick-entry") || action.equals("account.erase"))) {
                // Invalidate immediately, including an ASR call still waiting on its network response.
                voiceAuthEpoch.incrementAndGet();
                runOnPageUi(request, MainActivity.this::cancelVoice);
                final String previousChat = activeChatOperation;
                if (!previousChat.isEmpty()) {
                    cancelledChatOperation = previousChat;
                    chatControlWorker.execute(() -> {
                        try { if (api != null) api.callAttr("cancel_chat",previousChat); }
                        catch (Exception ignored) { }
                    });
                }
            }
            final String memberAction=businessParams==null?"":businessParams.optString("action");
            final boolean inferenceRequest=chatRequest || java.util.Arrays.asList("voice.records.propose","voice.smart-record",
                "mobile.image.analyze","expansion.analyses.run","ai.statistics.explain","voice.polish").contains(memberAction);
            final boolean readRequest=NativeReadLanePolicy.readOnly(action,memberAction);
            final boolean onlineReadRequest=!readRequest && NativeOnlineReadLanePolicy.readOnly(action,memberAction);
            final boolean uploadRequest=NativeUploadLanePolicy.upload(action,memberAction);
            final boolean reminderRequest=NativeReminderLanePolicy.localToggle(action,memberAction);
            try { (action.startsWith("native.")?mediaWorker:inferenceRequest?aiWorker:reminderRequest?reminderWorker:readRequest?readWorker:onlineReadRequest?onlineReadWorker:uploadRequest?uploadWorker:worker).execute(() -> {
                try {
                    if (!isCurrent() || !trustedPage || !pageSessions.accepts(request)) return;
                    if ((readRequest || onlineReadRequest || uploadRequest || reminderRequest) && requestAccountEpoch != voiceAuthEpoch.get()) {
                        failure(request,"account_changed","登录状态已变化，请重新读取。");return;
                    }
                    if (pickerBusy) { failure(request, "picker_busy", "请先完成或取消当前图片选择。"); return; }
                    if (api == null) { failure(request, "not_ready", "会员服务尚未就绪，请重新打开应用。"); return; }
                    if (chatRequest && chatOperation.equals(cancelledChatOperation)) {
                        failure(request,"cancelled","已停止回答。");return;
                    }
                    if (action.startsWith("native.")) nativeAction(request);
                    else {
                        JSONObject params = request.optJSONObject("params");
                        boolean chat = ("member.request".equals(action)||"guest.request".equals(action)) && params != null
                            && "chat.send".equals(params.optString("action"));
                        JSONObject response = new JSONObject(chat
                            ? api.callAttr("dispatch", request.toString(), new ChatProgress(request, requestAccountEpoch)).toString()
                            : api.callAttr("dispatch", request.toString()).toString());
                        if ((readRequest || onlineReadRequest || uploadRequest || reminderRequest) && requestAccountEpoch != voiceAuthEpoch.get()) {
                            failure(request,"account_changed","登录状态已变化，请重新读取。");return;
                        }
                        if (readRequest || onlineReadRequest || uploadRequest || reminderRequest)
                            deliver(response, pageSessions.origin(request), requestAccountEpoch);
                        else deliver(response, pageSessions.origin(request));
                    }
                } catch (Exception ignored) {
                    if (action.equals("native.pickImage") || action.equals("native.pickFile")) { cleanupCamera(); pickerBusy = false; pendingPicker = null; }
                    failure(request, "native_error", "操作未完成，请检查输入后重试。");
                } finally {
                    synchronized (MainActivity.this) {
                        if (chatRequest && chatOperation.equals(activeChatOperation)) activeChatOperation = "";
                        if (chatRequest && chatOperation.equals(cancelledChatOperation)) cancelledChatOperation = "";
                    }
                }
            }); } catch (RejectedExecutionException rejected) {
                failure(request,uploadRequest?"upload_busy":"read_busy",uploadRequest?"图片或文件正在上传，请稍后重试。":"本机资料正在读取，请稍后重试。");
            }
        }
    }

    private void nativeAction(JSONObject request) throws Exception {
        if (!pageSessions.accepts(request)) return;
        JSONObject params = request.getJSONObject("params");
        switch (request.getString("action")) {
            case "native.permissions.status": {
                requireFields(params);success(request,permissionStatus());break;
            }
            case "native.permissions.request": {
                requireFields(params,"kinds");
                JSONArray input=params.getJSONArray("kinds");
                List<Object> values=new ArrayList<>();
                for(int i=0;i<input.length();i++)values.add(input.get(i));
                Set<String> kinds=NativePermissionPolicy11616.kinds(values);
                runOnPageUi(request,()->requestAppPermissions(request,kinds));break;
            }
            case "native.permissions.openSettings": {
                requireFields(params);
                runOnPageUi(request,()-> {
                    try {
                        startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName())));
                        success(request,new JSONObject().put("opened",true));
                    }catch(Exception ignored){failure(request,"permission_unavailable","请在手机设置中查看凡思权限。");}
                });break;
            }
            case "native.local149.capabilities": {
                requireFields(params);success(request,LocalReminders149.capabilities(this));break;
            }
            case "native.local149.notifications": {
                requireFields(params);
                runOnPageUi(request, ()-> {
                    try {
                        if(entryPermissionsPending||specialSettingsPending){failure(request,"permission_busy","请先完成当前系统权限设置。");return;}
                        android.content.SharedPreferences asked=getSharedPreferences("permission-guide11616",MODE_PRIVATE);
                        if(Build.VERSION.SDK_INT>=33 && NativePermissionPolicy11616.requestable(
                                checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)==PackageManager.PERMISSION_GRANTED,
                                asked.getBoolean("notifications",false),shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS))) {
                            entryPermissionsPending=true;notificationRuntimePending=true;asked.edit().putBoolean("notifications",true).putBoolean("notifications_entry11827",true).apply();
                            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},14941);
                        }else openNotificationSettings();
                        success(request,new JSONObject().put("requested",true));
                    }catch(Exception ignored) {entryPermissionsPending=false;notificationRuntimePending=false;failure(request,"permission_unavailable","请在手机设置中开启凡思通知。");}
                });break;
            }
            case "native.local149.exactAlarms": {
                requireFields(params);
                runOnPageUi(request, ()-> {
                    try {
                        if(entryPermissionsPending||specialSettingsPending){failure(request,"permission_busy","请先完成当前系统权限设置。");return;}
                        if(Build.VERSION.SDK_INT>=31 && !getSystemService(android.app.AlarmManager.class).canScheduleExactAlarms()) {
                            specialSettingsPending=true;
                            getSharedPreferences("permission-guide11616",MODE_PRIVATE).edit().putBoolean("exact_alarms",true).apply();
                            startActivity(new Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM,Uri.parse("package:"+getPackageName())));
                        }
                        success(request,new JSONObject().put("requested",true));
                    }catch(Exception ignored) {specialSettingsPending=false;failure(request,"permission_unavailable","请在手机设置中允许准时提醒。");}
                });break;
            }
            case "native.getInfo":
                requireFields(params);
                success(request, new JSONObject().put("platform", "Android").put("version", BuildConfig.VERSION_NAME)
                    .put("application_id", BuildConfig.APPLICATION_ID).put("android", Build.VERSION.RELEASE)
                    .put("offline_voice", false).put("cloud_voice", true).put("voice_processing_events", true)
                    .put("max_voice_seconds", 60).put("max_image_bytes", IMAGE_LIMIT)
                    .put("max_images", 4).put("max_document_bytes", DOCUMENT_LIMIT).put("file_picker", true).put("cloud_personal_backup", false));
                break;
            case "native.pickFile": {
                if(entryPermissionsPending||specialSettingsPending){failure(request,"permission_busy","请先完成当前系统权限设置。");return;}
                requireFields(params, "max_count", "purpose");
                if (params.optInt("max_count", 1) != 1 || !"ai".equals(params.optString("purpose", "ai"))) throw new IllegalArgumentException();
                JSONObject scope=nativeScope();int actor=scope.optInt("actor_id",0);
                if(!usableMediaScope(scope)){failure(request,"login_required","请先完成当前登录或清理。");return;}
                pendingPicker=request;pendingOwner=actor;pendingImageCount=1;pickerBusy=true;bindPickerScope(scope,PICK_FILES);
                final int pickerCode=pendingPickerCode;
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("*/*")
                    .addCategory(Intent.CATEGORY_OPENABLE).putExtra(Intent.EXTRA_ALLOW_MULTIPLE, false)
                    .putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"image/jpeg", "image/png", "image/webp", "application/pdf", "text/plain", "text/markdown", "text/csv", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"});
                runOnPageUi(request, () -> {
                    try { if (isCurrent() && pendingPicker==request && pendingPickerCode==pickerCode) startActivityForResult(intent, pickerCode); }
                    catch (RuntimeException ignored) { pickerBusy = false; pendingPicker = null; failure(request, "picker_unavailable", "无法打开系统文件选择器，请稍后重试。"); }
                });
                break;
            }
            case "native.pickImage": {
                if(entryPermissionsPending||specialSettingsPending){failure(request,"permission_busy","请先完成当前系统权限设置。");return;}
                requireFields(params, "max_count", "source", "purpose");
                int count = params.has("max_count") ? params.getInt("max_count") : 1;
                if (count < 1 || count > 4) throw new IllegalArgumentException();
                String purpose = params.optString("purpose", "general");
                if (!purpose.equals("general") && !purpose.equals("ai") || purpose.equals("ai") && count > 2) throw new IllegalArgumentException();
                String source = params.optString("source", "gallery");
                if (!source.equals("gallery") && !source.equals("camera")) throw new IllegalArgumentException();
                JSONObject scope=nativeScope();int actor=scope.optInt("actor_id",0);
                if(!usableMediaScope(scope)){failure(request,"login_required","请先完成当前登录或清理。");return;}
                pendingPicker=request;pendingOwner=actor;pendingImageCount=count;pickerBusy=true;bindPickerScope(scope,source.equals("camera")?CAPTURE_IMAGE:PICK_IMAGES);
                final int pickerCode=pendingPickerCode;
                if (source.equals("camera")) {
                    File directory = ("guest".equals(scope.optString("kind"))?new File(scope.getString("camera_root")):new File(getCacheDir(), "picked-images"));
                    if (!directory.isDirectory() && !directory.mkdirs()) throw new IllegalStateException();
                    pendingCameraFile = File.createTempFile("fawnsee-camera-", ".jpg", directory);
                    pendingCameraUri = FileProvider.getUriForFile(this, BuildConfig.APPLICATION_ID + ".images", pendingCameraFile);
                    Intent camera = new Intent(MediaStore.ACTION_IMAGE_CAPTURE).putExtra(MediaStore.EXTRA_OUTPUT, pendingCameraUri)
                        .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    camera.setClipData(ClipData.newRawUri("FAWNSEE photo", pendingCameraUri));
                    runOnPageUi(request, () -> launchCamera(request,camera,pickerCode));
                    break;
                }
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("image/*")
                    .addCategory(Intent.CATEGORY_OPENABLE).putExtra(Intent.EXTRA_ALLOW_MULTIPLE, count > 1)
                    .putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"image/jpeg", "image/png", "image/webp"});
                runOnPageUi(request, () -> {
                    try { if (isCurrent() && pendingPicker==request && pendingPickerCode==pickerCode) startActivityForResult(intent, pickerCode); }
                    catch (RuntimeException ignored) { pickerBusy = false; pendingPicker = null; failure(request, "picker_unavailable", "无法打开系统图片选择器。"); }
                });
                break;
            }
            case "native.shareText": {
                requireFields(params, "title", "text");
                String title = params.getString("title"), text = params.getString("text");
                if (title.isEmpty() || title.length() > 120 || text.isEmpty() || text.length() > 16000) throw new IllegalArgumentException();
                runOnPageUi(request, () -> {
                    try {
                        Intent share = new Intent(Intent.ACTION_SEND).setType("text/plain")
                                .putExtra(Intent.EXTRA_SUBJECT, title).putExtra(Intent.EXTRA_TEXT, text);
                        startActivity(Intent.createChooser(share, "分享凡思社区帖子"));
                        success(request, new JSONObject().put("opened", true));
                    } catch (Exception error) {
                        failure(request, "share_unavailable", "暂时无法打开系统分享，请使用复制帖子内容。");
                    }
                });
                break;
            }
            case "native.copyText": {
                requireFields(params, "text");
                String text = params.getString("text");
                if (text.isEmpty() || text.length() > 16000) throw new IllegalArgumentException();
                runOnPageUi(request, () -> {
                    ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
                    clipboard.setPrimaryClip(ClipData.newPlainText("FAWNSEE", text));
                    success(request, JSONObject.NULL);
                });
                break;
            }
            case "native.exportBackup":
            case "native.importBackup":
                failure(request, "feature_unavailable", "当前云服务没有会员个人备份与恢复接口，本次未导出或更改云端资料。");
                break;
            default: failure(request, "unsupported_action", "不支持此手机操作。");
        }
    }

    private JSONObject permissionStatus() throws Exception {
        boolean microphone=checkSelfPermission(Manifest.permission.RECORD_AUDIO)==PackageManager.PERMISSION_GRANTED;
        boolean camera=checkSelfPermission(Manifest.permission.CAMERA)==PackageManager.PERMISSION_GRANTED;
        boolean runtimeNotifications=Build.VERSION.SDK_INT>=33;
        boolean notificationPermission=!runtimeNotifications || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)==PackageManager.PERMISSION_GRANTED;
        android.content.SharedPreferences asked=getSharedPreferences("permission-guide11616",MODE_PRIVATE);
        return new JSONObject().put("microphone_granted",microphone)
            .put("camera_granted",camera)
            .put("notifications_enabled",LocalReminders149.notifications(this))
            .put("notification_runtime_permission_required",runtimeNotifications).put("camera_delegated",true)
            .put("microphone_requestable",NativePermissionPolicy11616.requestable(microphone,asked.getBoolean("microphone",false),shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO)))
            .put("camera_requestable",NativePermissionPolicy11616.requestable(camera,asked.getBoolean("camera",false),shouldShowRequestPermissionRationale(Manifest.permission.CAMERA)))
            .put("notifications_requestable",runtimeNotifications && NativePermissionPolicy11616.requestable(notificationPermission,asked.getBoolean("notifications",false),shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS)));
    }
    private void requestEntryPermissions() {
        if(!isCurrent() || !hasWindowFocus() || entryPermissionsPending || specialSettingsPending || pendingPermissionRequest!=null
                || voiceRequest!=null || voiceCapture!=null || voiceStarting || voiceProcessing || pickerBusy)return;
        try {
            JSONObject status=permissionStatus();
            android.content.SharedPreferences previous=getSharedPreferences("permission-guide11616",MODE_PRIVATE);
            android.content.SharedPreferences.Editor asked=previous.edit();
            String kind=NativePermissionPolicy11616.nextEntryKind(Build.VERSION.SDK_INT,
                status.optBoolean("microphone_granted"),previous.getBoolean("microphone",false),
                status.optBoolean("camera_granted"),previous.getBoolean("camera",false),
                Build.VERSION.SDK_INT<33||checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)==PackageManager.PERMISSION_GRANTED,
                previous.getBoolean("notifications",false),Build.VERSION.SDK_INT>=33&&shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS),
                previous.getBoolean("notifications_entry11827",false));
            if(kind==null) {
                if(NativePermissionPolicy11616.notificationSettingsAtEntry(false,
                        previous.getBoolean("notification_channel_entry11838",false)||notificationSettingsAttemptedAtEntry,
                        notificationDeniedAtEntry)) {
                    // Banner/lockscreen behavior is controlled by the real OS
                    // channel page, never by an overlay or a fabricated grant.
                    openNotificationSettings();return;
                }
                JSONObject reminders=LocalReminders149.capabilities(this);
                if(reminders.optBoolean("notifications_enabled")&&NativeReminderDeliveryPolicy11827.requestExactAtEntry(Build.VERSION.SDK_INT,
                        reminders.optBoolean("exact_alarms_enabled"),remindersPrepared&&reminders.optBoolean("scheduled_reminders_present"),
                        previous.getBoolean("exact_alarms",false))) {
                    // Exact timing is a special system settings grant. Request
                    // only for saved reminders, once, after runtime dialogs close.
                    specialSettingsPending=true;asked.putBoolean("exact_alarms",true).apply();
                    startActivity(new Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM,Uri.parse("package:"+getPackageName())));
                }
                return;
            }
            // Android owns the actual prompt. Remember the attempt before it opens;
            // denial does not trigger a new request on every entry or long press.
            String permission="microphone".equals(kind)?Manifest.permission.RECORD_AUDIO:"camera".equals(kind)?Manifest.permission.CAMERA:Manifest.permission.POST_NOTIFICATIONS;
            entryPermissionsPending=true;asked.putBoolean(kind,true);
            if("notifications".equals(kind)){notificationRuntimePending=true;asked.putBoolean("notifications_entry11827",true);}
            asked.apply();requestPermissions(new String[]{permission},APP_PERMISSIONS);
        }catch(Exception error){entryPermissionsPending=false;specialSettingsPending=false;notificationRuntimePending=false;}
    }
    private void requestAppPermissions(JSONObject request,Set<String> kinds) {
        if(entryPermissionsPending || specialSettingsPending || pendingPermissionRequest!=null || voiceRequest!=null || voiceCapture!=null || voiceStarting || voiceProcessing || pickerBusy) {
            failure(request,"media_busy","请先完成当前拍照或语音操作。");return;
        }
        try {
            JSONObject status=permissionStatus();List<String> missing=new ArrayList<>();
            android.content.SharedPreferences.Editor asked=getSharedPreferences("permission-guide11616",MODE_PRIVATE).edit();
            if(kinds.contains("microphone") && status.optBoolean("microphone_requestable")) {missing.add(Manifest.permission.RECORD_AUDIO);asked.putBoolean("microphone",true);}
            if(kinds.contains("camera") && status.optBoolean("camera_requestable")) {missing.add(Manifest.permission.CAMERA);asked.putBoolean("camera",true);}
            if(kinds.contains("notifications") && status.optBoolean("notifications_requestable")) {missing.add(Manifest.permission.POST_NOTIFICATIONS);notificationRuntimePending=true;asked.putBoolean("notifications",true).putBoolean("notifications_entry11827",true);}
            if(missing.isEmpty()) {success(request,status.put("requested",false));return;}
            // Compatibility hook for an explicit native request; no custom guide.
            pendingPermissionRequest=request;asked.apply();
            requestPermissions(missing.toArray(new String[0]),APP_PERMISSIONS);
        }catch(Exception ignored){pendingPermissionRequest=null;notificationRuntimePending=false;failure(request,"permission_unavailable","请在手机设置中查看凡思权限。");}
    }

    private void openNotificationSettings() {
        notificationSettingsAttemptedAtEntry=true;specialSettingsPending=true;
        boolean opened=NativePermissionPolicy11616.openNotificationSettings(Build.VERSION.SDK_INT,LocalReminders149.notifications(this),target->{
            Intent intent;
            if("channel".equals(target))intent=new Intent(Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE,getPackageName())
                .putExtra(Settings.EXTRA_CHANNEL_ID,LocalReminders149.channelId(this));
            else if("app".equals(target))intent=new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE,getPackageName());
            else intent=new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName()));
            if(intent.resolveActivity(getPackageManager())==null)return false;
            startActivity(intent);return true;
        });
        if(!opened){specialSettingsPending=false;throw new IllegalStateException("System notification settings unavailable");}
        // A missing/blocked OEM activity must not consume the persistent once marker.
        getSharedPreferences("permission-guide11616",MODE_PRIVATE).edit().putBoolean("notification_channel_entry11838",true).apply();
    }
    private void launchCamera(JSONObject request,Intent camera,int pickerCode) {
        if(!isCurrent()||pendingPicker!=request||pendingPickerCode!=pickerCode)return;
        try {
            if(checkSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED) {
                android.content.SharedPreferences asked=getSharedPreferences("permission-guide11616",MODE_PRIVATE);
                if(!NativePermissionPolicy11616.requestable(false,asked.getBoolean("camera",false),shouldShowRequestPermissionRationale(Manifest.permission.CAMERA))) {
                    cleanupCamera();pickerBusy=false;pendingPicker=null;failure(request,"permission_denied","相机未开启，可在手机系统设置中允许凡思拍照，也可从相册选择。");return;
                }
                cameraAfterPermission=camera;asked.edit().putBoolean("camera",true).apply();
                requestPermissions(new String[]{Manifest.permission.CAMERA},CAMERA_PERMISSION);return;
            }
            startActivityForResult(camera,pickerCode);
        }catch(RuntimeException ignored){cameraAfterPermission=null;cleanupCamera();pickerBusy=false;pendingPicker=null;failure(request,"camera_unavailable","无法打开系统相机，请从相册选择照片。");}
    }

    private JSONObject nativeScope() throws Exception {
        PyObject current=api;return current==null?new JSONObject():new JSONObject(current.callAttr("native_scope11614").toString());
    }
    private boolean usableMediaScope(JSONObject scope) {
        return GuestScopePolicy11614.usable(scope.optString("kind"),scope.optString("scope_id"),scope.optString("session_epoch"),scope.optInt("actor_id",0),scope.optBoolean("cleanup_pending",false));
    }
    private boolean currentMediaScope(String id,String epoch) throws Exception {
        JSONObject current=nativeScope();return usableMediaScope(current)&&GuestScopePolicy11614.same(id,epoch,current.optString("scope_id"),current.optString("session_epoch"),current.optBoolean("cleanup_pending",false));
    }
    private static void addGuestScope(JSONObject value,String id,String epoch) throws Exception {
        if(id.startsWith("guest:")){value.put("scope_id",id);value.put("session_epoch",epoch);}
    }
    private void bindPickerScope(JSONObject scope,int kind) {
        pendingScopeId=scope.optString("scope_id");pendingScopeEpoch=scope.optString("session_epoch");pendingPrivateRoot=scope.optString("private_root");pendingPickerKind=kind;
        if(++pickerSequence>65000)pickerSequence=1001;pendingPickerCode=pickerSequence;
    }
    private int memberId() throws Exception {
        JSONObject reply = new JSONObject(api.callAttr("dispatch", "{\"id\":0,\"action\":\"auth.state\",\"params\":{}}").toString());
        JSONObject result = reply.optJSONObject("result");
        JSONObject user = result == null ? null : result.optJSONObject("user");
        return reply.optBoolean("ok") && user != null && "member".equals(user.optString("role_code")) ? user.optInt("id", 0) : 0;
    }
    private static void requireFields(JSONObject object, String... names) throws Exception {
        Set<String> allowed = new HashSet<>();
        java.util.Collections.addAll(allowed, names);
        for (java.util.Iterator<String> it = object.keys(); it.hasNext();) if (!allowed.contains(it.next())) throw new IllegalArgumentException();
    }

    @Override protected void onActivityResult(int code, int resultCode, Intent intent) {
        super.onActivityResult(code, resultCode, intent);
        if(code<1001 || code>65000 || code!=pendingPickerCode)return;
        final int kind=pendingPickerKind;final String scopeId=pendingScopeId,scopeEpoch=pendingScopeEpoch;
        JSONObject request = pendingPicker; int owner = pendingOwner, limit = pendingImageCount;
        pendingPicker = null;
        if (request == null) { pickerBusy = false; cleanupCamera(); eventText("pickerInterrupted", "图片选择因应用重开而中断，请重新选择。"); return; }
        if (resultCode != RESULT_OK || kind != CAPTURE_IMAGE && intent == null) { pickerBusy = false; cleanupCamera(); failure(request, "cancelled", "已取消选择。"); return; }
        List<Uri> uris = new ArrayList<>();
        if (kind == CAPTURE_IMAGE && pendingCameraUri != null) {
            uris.add(pendingCameraUri);
        } else if (intent.getClipData() != null) {
            ClipData clips = intent.getClipData();
            for (int i = 0; i < clips.getItemCount(); i++) uris.add(clips.getItemAt(i).getUri());
        } else if (intent.getData() != null) uris.add(intent.getData());
        mediaWorker.execute(() -> {
            try {
                if (!isCurrent()) return;
                if (!currentMediaScope(scopeId,scopeEpoch)) { failure(request, "account_changed", "使用状态已变更，请重新选择图片。"); return; }
                if (uris.isEmpty() || uris.size() > limit) { failure(request, "validation", "选择的图片数量超出本次上限。"); return; }
                JSONArray images = new JSONArray();
                boolean forAi = "ai".equals(request.getJSONObject("params").optString("purpose"));
                for (Uri uri : uris) images.put(kind == PICK_FILES ? readDocument(uri) : kind == CAPTURE_IMAGE ? readCamera(forAi) : readImage(uri, forAi));
                if(!currentMediaScope(scopeId,scopeEpoch)){failure(request,"account_changed","使用状态已变更，请重新选择图片。");return;}
                JSONObject media=new JSONObject().put(kind == PICK_FILES ? "files" : "images", images);
                addGuestScope(media,scopeId,scopeEpoch);success(request,media);
            } catch (Exception ignored) { failure(request, kind == PICK_FILES ? "invalid_file" : "invalid_image", kind == PICK_FILES ? "请选择完整的 JPG、PNG、WEBP、PDF、TXT、MD、CSV 或 DOCX 文件。图片不超过 2 MiB，文档不超过 5 MiB；扫描 PDF 请改用照片。" : "请选择完整的 JPG、PNG 或 WEBP 图片，每张不超过 2 MiB。"); }
            finally { cleanupCamera(); pickerBusy = false; }
        });
    }
    private JSONObject readCamera(boolean forAi) throws Exception {
        if (pendingCameraFile == null || !pendingCameraFile.isFile() || pendingCameraFile.length() > 40L * 1024 * 1024) throw new IllegalArgumentException();
        if (forAi) return AiImages.fromCamera(pendingCameraFile);
        Bitmap bitmap = BoundedImage.decode(pendingCameraFile.getAbsolutePath(), 2560);
        byte[] bytes = null;
        try {
            for (int quality : new int[]{88, 75, 60, 45}) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                if (!bitmap.compress(Bitmap.CompressFormat.JPEG, quality, out)) throw new IllegalArgumentException();
                bytes = out.toByteArray();
                if (bytes.length <= IMAGE_LIMIT) break;
            }
        } finally { bitmap.recycle(); }
        if (bytes == null || bytes.length > IMAGE_LIMIT) throw new IllegalArgumentException();
        return new JSONObject().put("name", "FAWNSEE-photo.jpg").put("media_type", "image/jpeg")
            .put("data_base64", Base64.encodeToString(bytes, Base64.NO_WRAP)).put("size_bytes", bytes.length);
    }
    private void cleanupCamera() {
        cameraAfterPermission=null;
        if (pendingCameraUri != null) { try { revokeUriPermission(pendingCameraUri, Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION); } catch (RuntimeException ignored) {} }
        if (pendingCameraFile != null && pendingCameraFile.isFile()) pendingCameraFile.delete();
        pendingCameraFile = null; pendingCameraUri = null;
    }
    private JSONObject readImage(Uri uri, boolean forAi) throws Exception {
        if (uri == null || !"content".equals(uri.getScheme())) throw new IllegalArgumentException();
        byte[] bytes;
        try (InputStream input = getContentResolver().openInputStream(uri); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            if (input == null) throw new IllegalArgumentException();
            transfer(input, out, IMAGE_LIMIT); bytes = out.toByteArray();
        }
        String type = imageType(bytes);
        BitmapFactory.Options bounds = new BitmapFactory.Options(); bounds.inJustDecodeBounds = true;
        BitmapFactory.decodeByteArray(bytes, 0, bytes.length, bounds);
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0 || (long) bounds.outWidth * bounds.outHeight > 25_000_000L) throw new IllegalArgumentException();
        String suffix = type.equals("image/jpeg") ? ".jpg" : type.equals("image/png") ? ".png" : ".webp";
        String name = displayName(uri).replaceAll("[\\\\/:\\p{Cntrl}]", "_");
        int dot = name.lastIndexOf('.'); if (dot > 0) name = name.substring(0, dot);
        if (name.length() > 100) name = name.substring(0, 100);
        if (name.trim().isEmpty()) name = "照片";
        if (forAi) return AiImages.fromBytes(bytes, name);
        return new JSONObject().put("name", name + suffix).put("media_type", type)
            .put("data_base64", Base64.encodeToString(bytes, Base64.NO_WRAP)).put("size_bytes", bytes.length);
    }
    private JSONObject readDocument(Uri uri) throws Exception {
        if (uri == null || !"content".equals(uri.getScheme())) throw new IllegalArgumentException();
        String name = displayName(uri).replaceAll("[\\\\/:\\p{Cntrl}]", "_");
        if (name.length() > 180 || name.trim().isEmpty()) throw new IllegalArgumentException();
        String lower = name.toLowerCase(java.util.Locale.ROOT);
        if (lower.endsWith(".jpg") || lower.endsWith(".jpeg") || lower.endsWith(".png") || lower.endsWith(".webp")) return readImage(uri, true);
        String media;
        if (lower.endsWith(".pdf")) media = "application/pdf";
        else if (lower.endsWith(".txt")) media = "text/plain";
        else if (lower.endsWith(".md") || lower.endsWith(".markdown")) media = "text/markdown";
        else if (lower.endsWith(".csv")) media = "text/csv";
        else if (lower.endsWith(".docx")) media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
        else throw new IllegalArgumentException();
        byte[] bytes;
        try (InputStream input = getContentResolver().openInputStream(uri); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            if (input == null) throw new IllegalArgumentException();
            transfer(input, out, DOCUMENT_LIMIT); bytes = out.toByteArray();
        }
        if (bytes.length == 0) throw new IllegalArgumentException();
        if (media.equals("application/pdf")) {
            if (bytes.length < 8 || bytes[0] != '%' || bytes[1] != 'P' || bytes[2] != 'D' || bytes[3] != 'F' || bytes[4] != '-') throw new IllegalArgumentException();
        } else if (media.startsWith("application/vnd")) {
            boolean documentXml = false, contentTypes = false;
            int entries = 0; long expanded = 0;
            try (java.util.zip.ZipInputStream zip = new java.util.zip.ZipInputStream(new java.io.ByteArrayInputStream(bytes))) {
                java.util.zip.ZipEntry entry; byte[] buffer = new byte[32768]; int n;
                while ((entry = zip.getNextEntry()) != null) {
                    if (++entries > 1500 || entry.getName().contains("..") || entry.getName().startsWith("/")) throw new IllegalArgumentException();
                    documentXml |= entry.getName().equals("word/document.xml");
                    contentTypes |= entry.getName().equals("[Content_Types].xml");
                    while ((n = zip.read(buffer)) != -1) { expanded += n; if (expanded > 20L * 1024 * 1024) throw new IllegalArgumentException(); }
                }
            }
            if (!documentXml || !contentTypes) throw new IllegalArgumentException();
        } else {
            java.nio.charset.StandardCharsets.UTF_8.newDecoder().onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
                .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT).decode(java.nio.ByteBuffer.wrap(bytes));
            for (byte value : bytes) if (value == 0) throw new IllegalArgumentException();
        }
        return new JSONObject().put("name", name).put("media_type", media)
            .put("data_base64", Base64.encodeToString(bytes, Base64.NO_WRAP)).put("size_bytes", bytes.length);
    }
    static String imageType(byte[] bytes) {
        if (bytes == null || bytes.length < 12 || bytes.length > IMAGE_LIMIT) throw new IllegalArgumentException();
        if ((bytes[0] & 255) == 255 && (bytes[1] & 255) == 216 && (bytes[2] & 255) == 255) return "image/jpeg";
        if ((bytes[0] & 255) == 137 && bytes[1] == 80 && bytes[2] == 78 && bytes[3] == 71
                && bytes[4] == 13 && bytes[5] == 10 && bytes[6] == 26 && bytes[7] == 10) return "image/png";
        if (bytes[0] == 82 && bytes[1] == 73 && bytes[2] == 70 && bytes[3] == 70
                && bytes[8] == 87 && bytes[9] == 69 && bytes[10] == 66 && bytes[11] == 80) return "image/webp";
        throw new IllegalArgumentException();
    }
    private String displayName(Uri uri) {
        try (Cursor cursor = getContentResolver().query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) return cursor.getString(0);
        } catch (RuntimeException ignored) { }
        return "照片";
    }
    private static void transfer(InputStream source, OutputStream target, long limit) throws Exception {
        byte[] buffer = new byte[65536]; long total = 0; int count;
        while ((count = source.read(buffer)) != -1) {
            total += count; if (total > limit) throw new IllegalArgumentException();
            target.write(buffer, 0, count);
        }
        target.flush();
    }

    private void requestVoiceStart(JSONObject request) {
        if (!isCurrent()) return;
        if(entryPermissionsPending||specialSettingsPending){failure(request,"permission_busy","请先完成当前系统权限设置。");return;}
        if (pickerBusy) { failure(request, "picker_busy", "请先完成或取消当前图片选择。"); return; }
        final int seconds;
        try {
            JSONObject params = request.getJSONObject("params");
            requireFields(params, "max_seconds");
            Object value = params.opt("max_seconds");
            if (value != null && !(value instanceof Integer || value instanceof Long)) throw new IllegalArgumentException();
            long requested = value == null ? 60 : ((Number) value).longValue();
            if (requested < 1 || requested > 60) throw new IllegalArgumentException();
            seconds = (int) requested;
        } catch (Exception ignored) { failure(request, "invalid_request", "录音时长应在1至60秒之间。"); return; }
        if (voiceCapture != null || voiceStarting || voiceProcessing || voiceRequest != null) {
            failure(request, "voice_busy", "请先完成或取消当前语音。"); return;
        }
        final int generation = ++voiceGeneration;
        final long accountEpoch = voiceAuthEpoch.get();
        voiceOwner = 0; voiceAccountEpoch = accountEpoch;
        voiceText = ""; voiceCompleted = null; voiceRequest = request; voiceStarting = true;
        mediaWorker.execute(() -> {
            if (!currentVoice(generation, accountEpoch)) return;
            try {
                JSONObject scope=nativeScope();int owner=scope.optInt("actor_id",0);
                runOnUiThread(() -> {
                    if (!currentVoice(generation, accountEpoch)) return;
                    if(!usableMediaScope(scope)){failVoice(generation,"login_required","请先完成当前登录或清理。");return;}
                    voiceOwner=owner;voiceScopeId=scope.optString("scope_id");voiceScopeEpoch=scope.optString("session_epoch");voicePrivateRoot=scope.optString("private_root");
                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                        if(!NativePermissionPolicy11616.requestable(false,getSharedPreferences("permission-guide11616",MODE_PRIVATE).getBoolean("microphone",false),shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO))){
                            failVoice(generation,"permission_denied","麦克风未开启，可在手机系统设置中允许凡思录音。");return;
                        }
                        awaitingMicPermission = true;
                        try { getSharedPreferences("permission-guide11616",MODE_PRIVATE).edit().putBoolean("microphone",true).apply();requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MIC_PERMISSION); }
                        catch (RuntimeException ignored) { failVoice(generation, "permission_denied", "请在系统设置中开启麦克风权限。"); }
                    } else beginVoice(seconds);
                });
            } catch (Exception ignored) {
                runOnUiThread(() -> { if (currentVoice(generation, accountEpoch)) failVoice(generation,
                    "not_ready", "语音服务尚未就绪，请稍后重试。"); });
            }
        });
    }

    private boolean currentVoice(int generation, long accountEpoch) {
        return isCurrent() && generation == voiceGeneration && accountEpoch == voiceAuthEpoch.get();
    }

    private void beginVoice(int seconds) {
        final int generation = voiceGeneration, owner = voiceOwner;
        final long accountEpoch = voiceAccountEpoch;
        if (!currentVoice(generation, accountEpoch)) { cancelVoice(); return; }
        File directory = new File(new File(voicePrivateRoot), "voice-capture");
        VoiceLevelUpdates levels = new VoiceLevelUpdates(this::runOnUiThread,
            () -> currentVoice(generation, accountEpoch) && trustedPage && webView != null
                && voiceCapture != null && !voiceStarting && !voiceProcessing,
            value -> {
                try { event("voiceLevel", new JSONObject().put("level", value).put("session_id", generation)); }
                catch (Exception ignored) { }
            }, type -> android.util.Log.w("FawnseeVoice", "voice_level_disabled:" + type));
        voiceCapture = new NativeVoiceCapture(directory, seconds, new NativeVoiceCapture.Listener() {
            @Override public void started() {
                runOnUiThread(() -> {
                    if (!currentVoice(generation, accountEpoch)) return;
                    if (!hasWindowFocus()) { cancelVoice(); return; }
                    voiceStarting = false;
                    // The capture callback follows the first real PCM sample.
                    try { if (webView != null) webView.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS); }
                    catch (RuntimeException ignored) { }
                    JSONObject request = voiceRequest; voiceRequest = null;
                    try {
                        if (request != null) success(request, new JSONObject().put("recording", true)
                            .put("max_seconds", seconds).put("session_id", generation));
                        event("voiceStarted", new JSONObject().put("text", "").put("session_id", generation));
                    } catch (Exception ignored) { failVoice(generation, "microphone_error", "录音未能开始，请重新按住说话。"); }
                });
            }
            @Override public void level(float value) {
                levels.offer(value);
            }
            @Override public void completed(File wav, int durationMillis) {
                runOnUiThread(() -> {
                    if (!currentVoice(generation, accountEpoch)) { wav.delete(); return; }
                    voiceCapture = null; voiceStarting = false; voiceProcessing = true;
                    voiceInFlightWav = wav;
                    try { event("voiceProcessing", new JSONObject().put("text", "").put("processing", true)
                        .put("session_id", generation).put("duration_ms", durationMillis)); }
                    catch (Exception ignored) { }
                    voiceWorker.execute(() -> transcribeVoice(wav, generation, owner, accountEpoch));
                });
            }
            @Override public void failed(String code) {
                runOnUiThread(() -> {
                    if (currentVoice(generation, accountEpoch)) failVoice(generation, code,
                        "voice_too_short".equals(code) ? "录音时间太短，请按住说完后再松开。" :
                        "permission_denied".equals(code) ? "未授予麦克风权限，可继续输入文字。" :
                        "无法录音，请检查麦克风权限或其他应用占用。");
                });
            }
        });
        voiceCapture.start();
    }

    private void transcribeVoice(File wav, int generation, int owner, long accountEpoch) {
        try {
            if (!currentVoice(generation, accountEpoch)) return;
            JSONObject reply;
            try {
                // This internal Python entry point owns authentication and accepts only private WAV paths.
                reply = new JSONObject((voiceScopeId.startsWith("guest:")
                    ? api.callAttr("transcribe_guest_voice_file",wav.getAbsolutePath(),voiceScopeId,voiceScopeEpoch,new VoiceProgress(generation,accountEpoch))
                    : api.callAttr("transcribe_voice_file", wav.getAbsolutePath(), owner,new VoiceProgress(generation, accountEpoch))).toString());
            } catch (Exception ignored) {
                reply = new JSONObject().put("ok", false).put("error", new JSONObject()
                    .put("code", "cloud_unavailable").put("message", "语音服务暂时无法连接。"));
            }
            if (!currentVoice(generation, accountEpoch)) return;
            // Python already rechecks the captured member/session after its request.
            // The immediate bridge auth epoch invalidates logout/login without queuing
            // another auth.state behind unrelated cloud requests on the business lock.
            JSONObject result;
            if (reply.optBoolean("ok")) {
                result = reply.optJSONObject("result");
                if (result == null) throw new IllegalStateException();
            } else {
                JSONObject error = reply.optJSONObject("error");
                String code = error == null ? "cloud_unavailable" : error.optString("code", "cloud_unavailable");
                String message = error == null ? "识别未完成，请重新录音或输入文字。" : error.optString("message", "识别未完成，请重新录音或输入文字。");
                runOnUiThread(() -> { if (currentVoice(generation, accountEpoch)) failVoice(generation, code, message); });
                return;
            }
            String text = VoiceResultPolicy.cleanText(result.optString("text", ""));
            if (text.isEmpty()) {
                runOnUiThread(() -> { if (currentVoice(generation, accountEpoch)) failVoice(generation, "voice_empty", "没有听清这段话，请重新录音，也可以直接输入文字。"); });
                return;
            }
            if (!currentVoice(generation, accountEpoch)) return;
            boolean offline = result.optBoolean("offline", false);
            String engine = result.optString("engine", "");
            String provider = result.optString("provider", "");
            String providerModel = result.optString("provider_model", "");
            if (!VoiceResultPolicy.isCloudResult(engine, provider, providerModel, offline))
                throw new IllegalStateException("voice_provider_contract");
            JSONObject completed = new JSONObject().put("text", text)
                .put("engine", engine).put("provider", provider).put("provider_model", providerModel)
                .put("offline", offline).put("session_id", generation);
            runOnUiThread(() -> {
                if (!currentVoice(generation, accountEpoch)) return;
                voiceProcessing = false; voiceInFlightWav = null; voiceText = text; voiceCompleted = completed;
                event("voiceStopped", completed);
                for (JSONObject request : voiceStopRequests) success(request, completed);
                voiceStopRequests.clear();
            });
        } catch (Exception ignored) {
            runOnUiThread(() -> { if (currentVoice(generation, accountEpoch)) failVoice(generation,
                "voice_unavailable", "识别未完成，请保留原有文字后重试。"); });
        } finally { wav.delete(); }
    }

    /** Internal Chaquopy callback; only genuine model output reaches voicePartial. */
    public final class VoiceProgress {
        private final int generation;
        private final long accountEpoch;
        VoiceProgress(int generation, long accountEpoch) {
            this.generation = generation; this.accountEpoch = accountEpoch;
        }
        public void onPartial(String text) {
            final String value = VoiceResultPolicy.cleanText(text);
            runOnUiThread(() -> {
                if (!currentVoice(generation, accountEpoch) || !voiceProcessing) return;
                voiceText = value;
                try { event("voicePartial", new JSONObject().put("text", value)
                    .put("processing", true).put("session_id", generation)); }
                catch (Exception ignored) { }
            });
        }
    }

    /** Only bounded, application-observed steps; no model reasoning crosses this callback. */
    public final class ChatProgress {
        private final long accountEpoch;
        private final long pageEpoch;
        private final String operationId;
        private final String scopeId,scopeEpoch;
        private final ChatStreamPolicy streamPolicy = new ChatStreamPolicy();
        ChatProgress(JSONObject request, long accountEpoch) {
            this.accountEpoch = accountEpoch;
            this.pageEpoch = pageSessions.origin(request);
            JSONObject params = request.optJSONObject("params");
            this.operationId = params == null ? "" : params.optString("request_id", "");
            String id="",epoch="";try{if("guest.request".equals(request.optString("action"))){JSONObject scope=nativeScope();id=scope.optString("scope_id");epoch=scope.optString("session_epoch");}}catch(Exception ignored){}scopeId=id;scopeEpoch=epoch;
        }
        public void onActivity(String text) {
            if (text == null || text.length() > 12000 || !operationId.matches("[0-9a-fA-F-]{36}")) return;
            final JSONObject activity;
            try {
                activity = new JSONObject(text);
                String status = activity.optString("status");
                if (activity.optInt("version") != 1 || !("working".equals(status) || "complete".equals(status) || "error".equals(status))
                        || activity.optJSONArray("steps") == null || activity.getJSONArray("steps").length() > 12) return;
            } catch (Exception ignored) { return; }
            runOnUiThread(() -> {
                if (!isCurrent() || !trustedPage || !pageSessions.accepts(pageEpoch) || accountEpoch != voiceAuthEpoch.get()) return;
                try { JSONObject value=new JSONObject().put("operation_id", operationId).put("activity", activity);addGuestScope(value,scopeId,scopeEpoch);event("chatActivity",value,pageEpoch); }
                catch (Exception ignored) { }
            });
        }

        /** Public status, tool receipts and answer deltas; never raw model reasoning. */
        public void onStream(String text) {
            if (text == null || text.length() > ChatStreamPolicy.MAX_EVENT_CHARS
                    || !operationId.matches("[0-9a-fA-F-]{36}")) return;
            final JSONObject stream;
            try {
                stream = new JSONObject(text);
                String type = stream.optString("type", "");
                String delta = stream.optString("delta", "");
                for (String field : new String[]{"seq", "conversation_id", "assistant_message_id"}) {
                    Object number = stream.opt(field);
                    if (!(number instanceof Integer || number instanceof Long)) return;
                }
                boolean privateReasoning = stream.has("reasoning_content") || stream.has("reasoning");
                if (!streamPolicy.accept(type, stream.optLong("seq", 0),
                        stream.optLong("conversation_id", 0), stream.optLong("assistant_message_id", 0),
                        delta.length(), text.length(), privateReasoning)) return;
                if ("start".equals(type) && operationId.equals(cancelledChatOperation)) {
                    // An early stop may arrive before Python registers its stream.
                    chatControlWorker.execute(() -> {
                        try { if (api != null) api.callAttr("cancel_chat",operationId); }
                        catch (Exception ignored) { }
                    });
                }
            } catch (Exception ignored) { return; }
            runOnUiThread(() -> {
                if (!isCurrent() || !trustedPage || !pageSessions.accepts(pageEpoch) || accountEpoch != voiceAuthEpoch.get()) return;
                try { JSONObject value=new JSONObject().put("operation_id", operationId).put("stream", stream);addGuestScope(value,scopeId,scopeEpoch);event("chatStream",value,pageEpoch); }
                catch (Exception ignored) { }
            });
        }
    }

    private void stopVoiceRequest(JSONObject request, boolean cancel) {
        try { requireFields(request.getJSONObject("params")); }
        catch (Exception ignored) { failure(request, "invalid_request", "录音操作格式不正确。"); return; }
        if (cancel) {
            cancelVoice();
            try { success(request, new JSONObject().put("text", "").put("cancelled", true)); }
            catch (Exception ignored) { }
            return;
        }
        if (awaitingMicPermission || voiceStarting) {
            cancelVoice(); failure(request, "cancelled", "录音尚未开始，请重新按住说话。"); return;
        }
        if (voiceCapture != null || voiceProcessing) {
            if (voiceStopRequests.size() >= 4) { failure(request, "voice_busy", "语音正在识别，请稍候。"); return; }
            voiceStopRequests.add(request);
            if (voiceCapture != null) voiceCapture.stop();
            return; // Keep the existing stop promise pending until the final text is available.
        }
        try { success(request, voiceCompleted != null ? voiceCompleted :
            new JSONObject().put("text", voiceText).put("cancelled", false).put("session_id", voiceGeneration)); }
        catch (Exception ignored) { }
    }

    private void failVoice(int generation, String code, String message) {
        if (generation != voiceGeneration) return;
        if (voiceCapture != null) voiceCapture.cancel();
        if (voiceInFlightWav != null) { voiceInFlightWav.delete(); voiceInFlightWav = null; }
        voiceCapture = null; voiceStarting = false; voiceProcessing = false; awaitingMicPermission = false;
        if (voiceRequest != null) failure(voiceRequest, code, message);
        voiceRequest = null;
        for (JSONObject request : voiceStopRequests) failure(request, code, message);
        voiceStopRequests.clear();
        try { event("voiceError", new JSONObject().put("text", message).put("message", message)
            .put("code", code).put("session_id", generation)); }
        catch (Exception ignored) { }
    }

    private void cancelVoice() {
        boolean active = voiceCapture != null || voiceStarting || voiceProcessing || voiceRequest != null;
        int previous = voiceGeneration++;
        if (voiceCapture != null) voiceCapture.cancel();
        // Removing the private capture also cancels Python's asynchronous upload,
        // so the single voice worker can accept a new recording promptly.
        if (voiceInFlightWav != null) { voiceInFlightWav.delete(); voiceInFlightWav = null; }
        voiceCapture = null; voiceStarting = false; voiceProcessing = false; awaitingMicPermission = false;
        if (voiceRequest != null) failure(voiceRequest, "cancelled", "录音准备已取消。");
        voiceRequest = null;
        for (JSONObject request : voiceStopRequests) failure(request, "cancelled", "本次语音已取消。");
        voiceStopRequests.clear(); voiceText = ""; voiceCompleted = null;
        if (active) try { event("voiceCancelled", new JSONObject().put("text", "").put("session_id", previous)); }
        catch (Exception ignored) { }
    }
    @Override public void onRequestPermissionsResult(int code, String[] permissions, int[] grants) {
        super.onRequestPermissionsResult(code, permissions, grants);
        if((code==APP_PERMISSIONS||code==14941)&&notificationRuntimePending) {
            notificationDeniedAtEntry=Build.VERSION.SDK_INT>=33&&checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED;
            notificationRuntimePending=false;
        }
        if(code==APP_PERMISSIONS||code==14941)for(int i=0;i<permissions.length;i++)
            if(Manifest.permission.POST_NOTIFICATIONS.equals(permissions[i]))notificationDeniedAtEntry=i>=grants.length||grants[i]!=PackageManager.PERMISSION_GRANTED;
        if (code == APP_PERMISSIONS) {
            entryPermissionsPending=false;
            JSONObject request=pendingPermissionRequest;pendingPermissionRequest=null;
            if(request!=null)try {success(request,permissionStatus().put("requested",true));}
            catch(Exception ignored){failure(request,"permission_unavailable","请在手机设置中查看凡思权限。");}
            LocalReminders149.refreshAsync(this);
            if(webView!=null)webView.post(this::requestEntryPermissions);return;
        }
        if (code == 14941) {entryPermissionsPending=false;LocalReminders149.refreshAsync(this);if(webView!=null)webView.post(this::requestEntryPermissions);return;}
        if(code==CAMERA_PERMISSION) {
            JSONObject request=pendingPicker;Intent camera=cameraAfterPermission;cameraAfterPermission=null;
            if(request==null||camera==null){if(webView!=null)webView.post(this::requestEntryPermissions);return;}
            if(grants.length>0&&grants[0]==PackageManager.PERMISSION_GRANTED)
                runOnPageUi(request,()->launchCamera(request,camera,pendingPickerCode));
            else{cleanupCamera();pickerBusy=false;pendingPicker=null;failure(request,"permission_denied","未授予相机权限，可以从相册选择照片。如此前已选图片，图片会继续保留。");}
            if(webView!=null)webView.post(this::requestEntryPermissions);return;
        }
        if (code != MIC_PERMISSION) return;
        awaitingMicPermission = false;
        if (voiceRequest == null) return;
        JSONObject request = voiceRequest;
        if (grants.length > 0 && grants[0] == PackageManager.PERMISSION_GRANTED)
            beginVoice(request.optJSONObject("params").optInt("max_seconds", 60));
        else failVoice(voiceGeneration, "permission_denied", "未授予麦克风权限，可继续输入文字。");
    }

    private void success(JSONObject request, Object result) {
        try { deliver(new JSONObject().put("id", request.opt("id")).put("ok", true).put("result", result), pageSessions.origin(request)); } catch (Exception ignored) { }
    }
    private void failure(JSONObject request, String code, String message) {
        try { deliver(new JSONObject().put("id", request.opt("id")).put("ok", false).put("error", new JSONObject().put("code", code).put("message", message)), pageSessions.origin(request)); } catch (Exception ignored) { }
    }
    private void deliver(JSONObject response, long pageEpoch) {
        deliver(response, pageEpoch, -1L);
    }
    private void deliver(JSONObject response, long pageEpoch, long accountEpoch) {
        runOnUiThread(() -> {
            if (isCurrent() && trustedPage && webView != null && pageSessions.accepts(pageEpoch)) {
                try {
                    JSONObject current = response;
                    if (accountEpoch >= 0 && accountEpoch != voiceAuthEpoch.get())
                        current = new JSONObject().put("id", response.opt("id")).put("ok", false)
                            .put("error", new JSONObject().put("code", "account_changed").put("message", "登录状态已变化，请重新读取。"));
                    String encoded = JSONObject.quote(current.toString());
                    webView.evaluateJavascript("window.FAWNSEE_NATIVE&&window.FAWNSEE_NATIVE.receive(JSON.parse(" + encoded + "))", null);
                }
                catch (org.json.JSONException error) { android.util.Log.w("FawnseeBridge", "scoped_delivery"); }
                catch (RuntimeException | LinkageError error) {
                    android.util.Log.w("FawnseeVoice", "web_delivery:" + error.getClass().getSimpleName());
                }
            }
        });
    }
    private void event(String name, JSONObject result) {
        event(name, result, pageSessions.current());
    }
    private void event(String name, JSONObject result, long pageEpoch) {
        if(name.startsWith("voice")){try{addGuestScope(result,voiceScopeId,voiceScopeEpoch);}catch(Exception ignored){return;}}
        try { deliver(new JSONObject().put("event", name).put("result", result), pageEpoch); } catch (Exception ignored) { }
    }
    private void runOnPageUi(JSONObject request, Runnable action) {
        runOnUiThread(() -> {
            if (isCurrent() && trustedPage && pageSessions.accepts(request)) action.run();
            else if (request == pendingPicker) {
                cleanupCamera(); pickerBusy = false; pendingPicker = null;
            }
        });
    }
    private void eventText(String name, String text) {
        try { event(name, new JSONObject().put("text", text)); } catch (Exception ignored) { }
    }
    private void handleBack() {
        WebView target = webView;
        long pageEpoch = pageSessions.current();
        if (target == null) { finish(); return; }
        try {
            target.evaluateJavascript("(function(){if(window.FAWNSEE_APP_UPDATE&&window.FAWNSEE_APP_UPDATE.isOpen){window.FAWNSEE_APP_UPDATE.close();return true}return window.FAWNSEE_NATIVE&&window.FAWNSEE_NATIVE.onBack?window.FAWNSEE_NATIVE.onBack():false})()", handled -> {
                if (!isCurrent() || webView != target || !pageSessions.accepts(pageEpoch)) return;
                if (!"true".equals(handled)) new AlertDialog.Builder(this).setMessage("退出应用？请先保存正在填写的内容。")
                    .setPositiveButton("退出", (dialog, which) -> finish()).setNegativeButton("取消", null).show();
            });
        } catch (RuntimeException | LinkageError error) {
            android.util.Log.w("FawnseeVoice", "web_back:" + error.getClass().getSimpleName());
        }
    }
    private void publishAppVisibility() {
        if (!isCurrent() || !trustedPage || webView == null) return;
        try { event("appVisibility", new JSONObject().put("foreground", appResumed)); }
        catch (org.json.JSONException ignored) { }
    }
    @Override protected void onPause() {
        appResumed = false;
        publishAppVisibility();
        super.onPause();
        // Never start an upload just because the user switches apps or locks the phone.
        if (!awaitingMicPermission && (voiceCapture != null || voiceStarting)) cancelVoice();
    }
    @Override protected void onStop() {
        super.onStop();
        // Returning from our own permission/media/settings page is still the
        // same entry. A later genuine foreground entry may offer settings once.
        if(!entryPermissionsPending&&!specialSettingsPending&&pendingPermissionRequest==null&&!pickerBusy&&!awaitingMicPermission) {
            notificationDeniedAtEntry=false;notificationSettingsAttemptedAtEntry=false;
        }
    }
    @Override protected void onResume() {
        super.onResume();
        appResumed = true;
        publishAppVisibility();
        specialSettingsPending=false;remindersPrepared=false;
        if(webView!=null)webView.post(this::requestEntryPermissions);
        if(api!=null) {
            LocalReminders149.refreshAsync(this);
            worker.execute(()-> {try {api.callAttr("on_foreground");}catch(Exception ignored) { }});
        }
    }
    @Override public void onWindowFocusChanged(boolean focused) {
        super.onWindowFocusChanged(focused);
        if(focused && webView!=null)webView.post(this::requestEntryPermissions);
    }
    private boolean isCurrent() { return !destroyed && instanceGeneration == instances.get(); }
    @Override protected void onDestroy() {
        if (appUpdater != null) appUpdater.close();
        NativeLocalHooks149.removeGuestInvalidator(guestInvalidator);voiceAuthEpoch.incrementAndGet(); cancelVoice(); cleanupCamera(); destroyed = true; trustedPage = false;
        final String previousChat = activeChatOperation;
        if (!previousChat.isEmpty()) {
            cancelledChatOperation = previousChat;
            chatControlWorker.execute(() -> {
                try { if (api != null) api.callAttr("cancel_chat",previousChat); }
                catch (Exception ignored) { }
            });
        }
        if(localReceiverRegistered) {unregisterReceiver(localReceiver);localReceiverRegistered=false;}
        voiceWorker.shutdown();
        mediaWorker.shutdownNow();pendingPermissionRequest=null;
        if (webView != null) { webView.removeJavascriptInterface("AndroidBridge"); webView.destroy(); }
        super.onDestroy();
    }
}

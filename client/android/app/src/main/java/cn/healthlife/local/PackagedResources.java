package cn.healthlife.local;

import android.content.res.AssetManager;
import android.net.Uri;
import android.webkit.WebResourceResponse;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

/** Exact build-generated allow-list. Python, models and private files are never served. */
final class PackagedResources {
    static final String ORIGIN = "https://appassets.androidplatform.net";
    static final String PREFIX = "/assets/www/";
    static final String CSP = "default-src 'none'; script-src 'self' 'unsafe-inline'; "
        + "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
        + "connect-src 'none'; frame-src 'self' about:; media-src 'self' data: blob:; "
        + "object-src 'none'; base-uri 'self'; form-action 'none'; worker-src 'none'";
    private final AssetManager assets;
    private final Set<String> allowed = new HashSet<>();

    PackagedResources(AssetManager assets) throws Exception {
        this.assets = assets;
        try (InputStream input = assets.open("mobile-resource-manifest.json")) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192]; int count;
            while ((count = input.read(buffer)) != -1) {
                out.write(buffer, 0, count);
                if (out.size() > 4 * 1024 * 1024) throw new IllegalArgumentException();
            }
            JSONObject manifest = new JSONObject(out.toString(StandardCharsets.UTF_8.name()));
            if (manifest.getInt("version") != 1) throw new IllegalArgumentException();
            JSONArray files = manifest.getJSONArray("files");
            if (files.length() > 20000) throw new IllegalArgumentException();
            for (int i = 0; i < files.length(); i++) {
                String path = files.getJSONObject(i).getString("path");
                if (!safePath(path) || !allowed.add(path)) throw new IllegalArgumentException();
            }
            if (!allowed.contains("index.html")) throw new IllegalArgumentException();
        }
    }
    static boolean safePath(String path) {
        if (path == null || path.isEmpty() || path.length() > 512 || !path.matches("[A-Za-z0-9_./-]+")) return false;
        for (String part : path.split("/", -1)) if (part.isEmpty() || part.equals(".") || part.equals("..")) return false;
        return true;
    }
    boolean isAllowed(Uri uri) {
        return uri != null && "https".equals(uri.getScheme())
            && "appassets.androidplatform.net".equals(uri.getHost()) && uri.getPort() == -1
            && uri.getUserInfo() == null && uri.getEncodedQuery() == null
            && uri.getPath() != null && uri.getPath().startsWith(PREFIX)
            && !uri.getEncodedPath().contains("%")
            && allowed.contains(uri.getPath().substring(PREFIX.length()));
    }
    WebResourceResponse load(Uri uri) {
        if (!isAllowed(uri)) return denied();
        String path = uri.getPath().substring(PREFIX.length());
        try {
            String mime = mime(path);
            WebResourceResponse response = new WebResourceResponse(mime,
                mime.startsWith("text/") || mime.contains("javascript") || mime.contains("json") || mime.contains("svg") ? "UTF-8" : null,
                assets.open("www/" + path));
            Map<String, String> headers = new HashMap<>();
            headers.put("Content-Security-Policy", CSP);
            headers.put("X-Content-Type-Options", "nosniff");
            headers.put("Referrer-Policy", "no-referrer");
            headers.put("Cache-Control", "no-store");
            response.setResponseHeaders(headers);
            return response;
        } catch (Exception ignored) { return denied(); }
    }
    static String mime(String path) {
        String lower = path.toLowerCase(java.util.Locale.ROOT);
        if (lower.endsWith(".html")) return "text/html";
        if (lower.endsWith(".css")) return "text/css";
        if (lower.endsWith(".js") || lower.endsWith(".mjs")) return "application/javascript";
        if (lower.endsWith(".json")) return "application/json";
        if (lower.endsWith(".svg")) return "image/svg+xml";
        if (lower.endsWith(".png")) return "image/png";
        if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
        if (lower.endsWith(".webp")) return "image/webp";
        if (lower.endsWith(".woff2")) return "font/woff2";
        if (lower.endsWith(".woff")) return "font/woff";
        if (lower.endsWith(".ttf")) return "font/ttf";
        if (lower.endsWith(".otf")) return "font/otf";
        return "application/octet-stream";
    }
    static WebResourceResponse denied() {
        return new WebResourceResponse("text/plain", "UTF-8", 403, "Forbidden",
            Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
    }
}

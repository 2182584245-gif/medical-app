package cn.healthlife.local;

import java.io.IOException;
import java.io.InputStream;
import java.net.Proxy;
import java.util.concurrent.TimeUnit;
import okhttp3.Authenticator;
import okhttp3.Call;
import okhttp3.CookieJar;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;

/** Separate standard HTTPS client: no business cookies, authentication, or redirects. */
final class UpdateTransport implements AutoCloseable {
    private static final OkHttpClient CLIENT = new OkHttpClient.Builder()
            .cookieJar(CookieJar.NO_COOKIES)
            .authenticator(Authenticator.NONE)
            .proxyAuthenticator(Authenticator.NONE)
            .proxy(Proxy.NO_PROXY)
            .followRedirects(false)
            .followSslRedirects(false)
            .retryOnConnectionFailure(false)
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(20, TimeUnit.SECONDS)
            .cache(null)
            .build();
    private Call call;
    private Response response;
    private boolean closed;

    static final class Reply {
        final InputStream input;
        final long length;
        Reply(InputStream input, long length) { this.input = input; this.length = length; }
    }

    Reply open(String value, long expectedLength, long maximum) throws IOException {
        ReleaseInfo.requireDownloadUrl(value);
        Request request = new Request.Builder().url(value)
                .header("Accept", "*/*")
                .header("Accept-Encoding", "identity")
                .header("Cache-Control", "no-cache")
                .header("User-Agent", "FAWNSEE-Mobile-Updater/1")
                .get().build();
        Call next = CLIENT.newCall(request);
        synchronized (this) {
            if (closed) throw new IOException("更新已取消。");
            call = next;
        }
        Response result = next.execute();
        synchronized (this) {
            if (closed) { result.close(); throw new IOException("更新已取消。"); }
            response = result;
        }
        // Standard HTTP framing stays in OkHttp. Final size and digest are also
        // checked by ReleaseInfo, including when a response is chunked.
        if (result.code() != 200) throw new IOException("更新服务暂不可用，请稍后重试。");
        String encoding = result.header("Content-Encoding");
        if (encoding != null && !"identity".equalsIgnoreCase(encoding)) throw badResponse();
        ResponseBody body = result.body();
        if (body == null) throw badResponse();
        long length = body.contentLength();
        if (length == 0 || length > maximum || (length >= 0 && expectedLength > 0 && length != expectedLength)) throw badResponse();
        return new Reply(body.byteStream(), length);
    }

    @Override public void close() {
        final Call activeCall;
        final Response activeResponse;
        synchronized (this) {
            closed = true;
            activeCall = call;
            activeResponse = response;
            call = null;
            response = null;
        }
        if (activeCall != null) activeCall.cancel();
        if (activeResponse != null) activeResponse.close();
    }
    private static IOException badResponse() { return new IOException("更新下载响应不完整，请重试。"); }
}

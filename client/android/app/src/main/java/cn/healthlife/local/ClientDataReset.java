package cn.healthlife.local;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;
import android.util.Log;
import android.webkit.CookieManager;
import android.webkit.WebStorage;
import java.io.IOException;
import java.io.File;
import java.util.ArrayList;
import java.util.List;

/** Explicit build-time account epoch; staged releases keep existing local data. */
final class ClientDataReset {
    interface Completion { void finish(Exception error); }
    private static final List<Completion> waiting=new ArrayList<>();
    private static boolean running;
    private ClientDataReset() { }
    static boolean isPrepared(boolean epochResetRequired,File files) throws IOException {
        return !epochResetRequired || ClientResetFiles.complete(files);
    }
    static synchronized void requireReady(Context context) throws IOException {
        if(!BuildConfig.ACCOUNT_EPOCH_RESET_REQUIRED)return;
        if(!isPrepared(true,context.getFilesDir()))throw new IOException("Open the app to finish local reset");
    }
    static void prepare(Context context,Completion callback) {
        // The server's staged policy does not grant a client permission to wipe
        // all accounts. This flag is fixed by the reviewed application build.
        if(!BuildConfig.ACCOUNT_EPOCH_RESET_REQUIRED) {callback.finish(null);return;}
        final Context app=context.getApplicationContext();
        synchronized(ClientDataReset.class) {
            try {if(isPrepared(true,app.getFilesDir())) {callback.finish(null);return;} }
            catch(Exception error) {callback.finish(failure("marker-check",error));return;}
            waiting.add(callback);if(running)return;running=true;
        }
        new Thread(()-> {
            String phase="stop-local-work";
            try {
                new NativeLocalHooks149(app).deactivate();
                phase="reminder-registry";
                if(!LocalReminders149.registry(app).edit().clear().commit())throw new IOException("Reminder reset unavailable");
                phase="private-files";
                ClientResetFiles.clearMemberFiles(app.getFilesDir(),app.getCacheDir(),app.getDataDir(),value-> {
                    try {return OsConstants.S_ISLNK(Os.lstat(value.getPath()).st_mode);}
                    catch(ErrnoException error) {
                        if(error.errno==OsConstants.ENOENT)return false;
                        throw new IOException("Private file metadata unavailable");
                    }
                });
                phase="actor-keys";
                LocalDataVault.clearAllActorKeys();
                new Handler(Looper.getMainLooper()).post(()-> {
                    String webPhase="web-storage";
                    try {
                        WebStorage.getInstance().deleteAllData();
                        webPhase="cookies";
                        CookieManager cookies=CookieManager.getInstance();
                        cookies.removeAllCookies(removed-> {
                            try {cookies.flush();ClientResetFiles.markComplete(app.getFilesDir());finish(null);}
                            catch(Exception error) {finish(failure("marker-commit",error));}
                        });
                    } catch(Exception error) {finish(failure(webPhase,error));}
                });
            } catch(Exception error) {
                Exception diagnostic=failure(phase,error);
                new Handler(Looper.getMainLooper()).post(()->finish(diagnostic));
            }
        },"fawnsee-client-reset").start();
    }
    private static Exception failure(String phase,Exception error) {
        // Fixed stage and type only: no user paths, file contents or credentials.
        Log.w("FawnseeReset",phase+":"+error.getClass().getSimpleName());
        return new IOException("Local setup step failed: "+phase);
    }
    private static void finish(Exception error) {
        List<Completion> callbacks;
        synchronized(ClientDataReset.class) {running=false;callbacks=new ArrayList<>(waiting);waiting.clear();}
        for(Completion callback:callbacks)callback.finish(error);
    }
}

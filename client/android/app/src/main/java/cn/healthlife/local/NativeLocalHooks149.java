package cn.healthlife.local;

import android.content.Context;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.webkit.WebStorage;
import android.webkit.CookieManager;
import android.webkit.WebView;
import androidx.work.BackoffPolicy;
import androidx.work.Constraints;
import androidx.work.ExistingPeriodicWorkPolicy;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.PeriodicWorkRequest;
import androidx.work.WorkManager;
import java.util.concurrent.TimeUnit;

/** Native callbacks for Python, never a browser interface. */
public final class NativeLocalHooks149 {
    private final Context context;
    public NativeLocalHooks149(Context context) { this.context=context.getApplicationContext(); }
    private static volatile Runnable guestInvalidator;
    static void installGuestInvalidator(Runnable value) {guestInvalidator=value;}
    static void removeGuestInvalidator(Runnable value) {if(guestInvalidator==value)guestInvalidator=null;}
    public void invalidateGuestScope() throws Exception {
        Runnable listener=guestInvalidator;if(listener==null)return;
        if(Looper.myLooper()==Looper.getMainLooper()){listener.run();return;}
        java.util.concurrent.CountDownLatch done=new java.util.concurrent.CountDownLatch(1);
        java.util.concurrent.atomic.AtomicReference<RuntimeException> failure=new java.util.concurrent.atomic.AtomicReference<>();
        new Handler(Looper.getMainLooper()).post(()->{try{if(guestInvalidator==listener)listener.run();}catch(RuntimeException error){failure.set(error);}finally{done.countDown();}});
        if(!done.await(10,TimeUnit.SECONDS))throw new java.io.IOException("Guest media cancellation not confirmed");
        if(failure.get()!=null)throw failure.get();
    }
    public void requestSync() {
        Constraints network=new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build();
        WorkManager manager=WorkManager.getInstance(context);
        manager.enqueueUniqueWork("fawnsee-local149-now",ExistingWorkPolicy.KEEP,
            new OneTimeWorkRequest.Builder(LocalSyncWorker149.class).setConstraints(network)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL,30,TimeUnit.SECONDS).build());
        manager.enqueueUniquePeriodicWork("fawnsee-local149-periodic",ExistingPeriodicWorkPolicy.KEEP,
            new PeriodicWorkRequest.Builder(LocalSyncWorker149.class,15,TimeUnit.MINUTES)
                .setConstraints(network).setBackoffCriteria(BackoffPolicy.EXPONENTIAL,30,TimeUnit.SECONDS).build());
    }
    public void remindersChanged() { LocalReminders149.refreshAsync(context); }
    public void syncFinished(String status) {
        if (status==null || status.length()>64000) return;
        context.sendBroadcast(new Intent(BuildConfig.APPLICATION_ID+".LOCAL_SYNC149")
            .setPackage(context.getPackageName()).putExtra("status",status));
    }
    public void deactivate() {
        WorkManager manager=WorkManager.getInstance(context);
        manager.cancelUniqueWork("fawnsee-local149-now");manager.cancelUniqueWork("fawnsee-local149-periodic");
        LocalReminders149.cancelAll(context);
    }
    public void clearAccountCaches() throws Exception {
        deactivate();
        java.io.File[] rows=context.getCacheDir().listFiles();
        if(rows==null)throw new java.io.IOException("Cache unavailable");
        for(java.io.File row:rows)if(!row.getName().equals("update"))ClientResetFiles.removeChild(context.getCacheDir(),row.getName());
        java.util.concurrent.CountDownLatch done=new java.util.concurrent.CountDownLatch(1);
        java.util.concurrent.atomic.AtomicReference<Exception> failure=new java.util.concurrent.atomic.AtomicReference<>();
        Runnable clear=()-> {
            try {
                WebStorage.getInstance().deleteAllData();
                WebView cleaner=new WebView(context);cleaner.clearCache(true);cleaner.destroy();
                CookieManager cookies=CookieManager.getInstance();
                cookies.removeAllCookies(removed-> {try {cookies.flush();}catch(Exception error){failure.set(error);}finally{done.countDown();}});
            } catch(Exception error) {failure.set(error);done.countDown();}
        };
        if(Looper.myLooper()==Looper.getMainLooper())throw new IllegalStateException("Account cleanup must run off the UI thread");
        new Handler(Looper.getMainLooper()).post(clear);
        if(!done.await(10,TimeUnit.SECONDS))throw new java.io.IOException("Web cleanup not confirmed");
        if(failure.get()!=null)throw failure.get();
    }
}

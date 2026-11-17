package cn.healthlife.local;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class LocalBootReceiver149 extends BroadcastReceiver {
    private static final ExecutorService executor=Executors.newSingleThreadExecutor();
    @Override public void onReceive(Context context,Intent intent) {
        String action=intent==null?null:intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action) && !Intent.ACTION_TIME_CHANGED.equals(action)
                && !Intent.ACTION_TIMEZONE_CHANGED.equals(action)
                && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)
                && !"android.app.action.SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED".equals(action)) return;
        // Android discards alarms at reboot and exact-alarm permission changes.
        // Invalidate our scheduling receipt before rebuilding the same dates.
        PendingResult pending=goAsync();
        Context app=context.getApplicationContext();
        executor.execute(()->{
            try { LocalReminders149.cancelAll(app);LocalReminders149.refresh(app,MobileRuntime149.module(app)); }
            catch(Exception ignored) { }
            finally { pending.finish(); }
        });
    }
}

package cn.healthlife.local;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.UiAutomation;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.net.Uri;
import android.os.Build;
import android.os.SystemClock;
import android.graphics.Bitmap;
import android.service.notification.StatusBarNotification;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import androidx.core.content.ContextCompat;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import static org.junit.Assert.*;
import static org.junit.Assume.*;

/** Owned emulator only. No account, Python database, network or private data. */
@RunWith(AndroidJUnit4.class)
public final class ReminderDelivery11827Test {
    private Context context(){return InstrumentationRegistry.getInstrumentation().getTargetContext();}

    @Test public void freshReminderChannelSupportsSystemHeadsUpWithoutAnOverlay() throws Exception {
        assumeTrue(Build.VERSION.SDK_INT>=26);
        Context app=context();NotificationManager manager=app.getSystemService(NotificationManager.class);
        NotificationChannel channel=manager.getNotificationChannel(LocalReminders149.channelId(app));
        assertNotNull(channel);assertEquals(NotificationManager.IMPORTANCE_HIGH,channel.getImportance());
        assertNotNull(channel.getSound());assertTrue(channel.shouldVibrate());
        assertFalse("No SYSTEM_ALERT_WINDOW declaration is needed for a system notification",java.util.Arrays.asList(
            app.getPackageManager().getPackageInfo(app.getPackageName(),android.content.pm.PackageManager.GET_PERMISSIONS).requestedPermissions
        ).contains("android.permission.SYSTEM_ALERT_WINDOW"));
    }
    @Test public void actualNativeNoticeHasPrioritySoundIntentAndPrivateText() {
        Notification note=LocalReminders149.notice(context(),"起身活动");
        assertEquals(Notification.PRIORITY_HIGH,note.priority);
        assertEquals(Notification.CATEGORY_REMINDER,note.category);
        assertEquals(Notification.VISIBILITY_PRIVATE,note.visibility);
        assertEquals("起身活动",note.extras.getString(Notification.EXTRA_TITLE));
        assertEquals("到时间了，打开凡思查看今天的安排。",note.extras.getString(Notification.EXTRA_TEXT));
        assertNotNull(note.contentIntent);assertNull(note.fullScreenIntent);
        assertNotNull(note.publicVersion);
        assertEquals(Notification.VISIBILITY_PUBLIC,note.publicVersion.visibility);
        assertEquals("凡思生活提醒",note.publicVersion.extras.getString(Notification.EXTRA_TITLE));
        assertFalse(note.publicVersion.extras.getString(Notification.EXTRA_TITLE).contains("起身活动"));
        assertEquals("有一条生活提醒，解锁后查看。",note.publicVersion.extras.getString(Notification.EXTRA_TEXT));
        assertTrue((note.flags&Notification.FLAG_AUTO_CANCEL)!=0);
    }
    @Test public void realAndroidAlarmPostsTheActualNativeNoticeWhenDue() throws Exception {
        Context app=context();AlarmManager alarms=app.getSystemService(AlarmManager.class);
        NotificationManager manager=app.getSystemService(NotificationManager.class);
        assumeTrue(LocalReminders149.notifications(app));
        assumeTrue(Build.VERSION.SDK_INT<31||alarms.canScheduleExactAlarms());
        String action=app.getPackageName()+".INSTRUMENTED_REMINDER11827";
        CountDownLatch fired=new CountDownLatch(1);AtomicLong received=new AtomicLong();
        BroadcastReceiver receiver=new BroadcastReceiver(){@Override public void onReceive(Context c,Intent i){
            if(action.equals(i.getAction())){received.set(System.currentTimeMillis());manager.notify("instrumented11827",11827,LocalReminders149.notice(app,"定时通知验证"));fired.countDown();}
        }};
        IntentFilter filter=new IntentFilter(action);
        filter.addDataScheme("fawnsee");
        filter.addDataAuthority("instrumented-reminder11827",null);
        ContextCompat.registerReceiver(app,receiver,filter,ContextCompat.RECEIVER_NOT_EXPORTED);
        PendingIntent pending=PendingIntent.getBroadcast(app,11827,new Intent(action).setPackage(app.getPackageName())
            .setData(Uri.parse("fawnsee://instrumented-reminder11827")),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        long due=System.currentTimeMillis()+2000;
        try {
            assertTrue(LocalReminders149.arm(alarms,due,pending));
            assertTrue("The Android alarm must actually fire",fired.await(12,TimeUnit.SECONDS));
            assertTrue(received.get()>=due-100);assertTrue(received.get()<=due+8000);
            StatusBarNotification actual=null;
            for(StatusBarNotification item:manager.getActiveNotifications())if("instrumented11827".equals(item.getTag()))actual=item;
            assertNotNull("NotificationManager must hold the posted native notification",actual);
            assertEquals("定时通知验证",actual.getNotification().extras.getString(Notification.EXTRA_TITLE));
            if(Build.VERSION.SDK_INT>=26)assertEquals(LocalReminders149.channelId(app),actual.getNotification().getChannelId());
            // Record the actual system window and screenshot, rather than
            // treating a HIGH channel as proof that an OEM displayed a banner.
            UiAutomation automation=InstrumentationRegistry.getInstrumentation().getUiAutomation();
            AccessibilityServiceInfo service=automation.getServiceInfo();int flags=service.flags;
            boolean banner=false;
            try {
                service.flags|=AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;automation.setServiceInfo(service);
                long until=SystemClock.elapsedRealtime()+3000;
                while(!banner&&SystemClock.elapsedRealtime()<until) {
                    for(AccessibilityWindowInfo window:automation.getWindows()) {
                        AccessibilityNodeInfo root=window.getRoot();
                        if(root!=null)for(AccessibilityNodeInfo match:root.findAccessibilityNodeInfosByText("定时通知验证"))
                            if(match.isVisibleToUser()&&"com.android.systemui".equals(String.valueOf(match.getPackageName()))){banner=true;break;}
                        if(banner)break;
                    }
                    if(!banner)SystemClock.sleep(80);
                }
                Bitmap screen=automation.takeScreenshot();
                if(screen!=null)try(FileOutputStream file=new FileOutputStream(new File(app.getExternalFilesDir(null),"heads-up11827.png"))) {
                    screen.compress(Bitmap.CompressFormat.PNG,100,file);file.flush();
                }
            }finally{service.flags=flags;automation.setServiceInfo(service);}
            JSONObject evidence=new JSONObject().put("android_alarm_fired",true).put("due_at",due)
                .put("received_at",received.get()).put("notification_posted",true)
                .put("system_banner_text_observed",banner).put("notification_channel",LocalReminders149.channelId(app))
                .put("overlay_permission_required",false).put("real_member_account_tested",false);
            try(FileOutputStream file=new FileOutputStream(new File(app.getExternalFilesDir(null),"native-reminder11827.json"))) {
                file.write(evidence.toString(2).getBytes(StandardCharsets.UTF_8));file.flush();
            }
        }finally{alarms.cancel(pending);pending.cancel();manager.cancel("instrumented11827",11827);app.unregisterReceiver(receiver);}
    }
}

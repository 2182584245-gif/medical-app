package cn.healthlife.local;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationManager;
import android.content.Context;
import android.graphics.Bitmap;
import android.os.Build;
import android.os.SystemClock;
import android.service.notification.StatusBarNotification;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import static org.junit.Assert.*;

/** Dedicated offline emulator, synthetic owner, real encrypted local projection.
 * Harness must set the emulator wall clock shortly before local09:00. This test
 * never changes the production clock or exports a test endpoint in the app. */
@RunWith(AndroidJUnit4.class)
public final class RoutineReminder11828Test {
    @Test public void legacyOrdinaryReceiptDoesNotRequireRoutineScopeOrGeneration() throws Exception {
        Context app=InstrumentationRegistry.getInstrumentation().getTargetContext();
        long at=System.currentTimeMillis();String key=LocalReminders149.key(991,"7701");
        LocalReminders149.cancelAll(app);
        LocalReminders149.registry(app).edit().putString(key,new JSONObject().put("actor",991).put("id","7701")
            .put("at",at).put("mode","exact").toString()).commit();
        try {
            assertTrue(LocalReminders149.postCurrent(app,991,"7701",at,"",-1,"","原有提醒验证","到时间了，打开凡思查看今天的安排。"));
            assertFalse(LocalReminders149.postCurrent(app,991,"7701",at,"",-1,"","原有提醒验证","到时间了，打开凡思查看今天的安排。"));
        }finally{LocalReminders149.cancelAll(app);}
    }
    @Test public void realRoutineReceiverRechecksLocalPythonAndPostsExactlyOneDailyNotice() throws Exception {
        Context app=InstrumentationRegistry.getInstrumentation().getTargetContext();
        AlarmManager alarms=app.getSystemService(AlarmManager.class);
        NotificationManager notifications=app.getSystemService(NotificationManager.class);
        assertTrue("Grant the real system notification permission before this test",LocalReminders149.notifications(app));
        assertTrue("Grant exact-alarm access to this isolated fixture",Build.VERSION.SDK_INT<31||alarms.canScheduleExactAlarms());
        PyObject api=MobileRuntime149.module(app);
        PyObject builtins=Python.getInstance().getModule("builtins");PyObject namespace=builtins.callAttr("dict");
        String script="import mobile_api as m, tempfile, shutil\n"
            +"from types import SimpleNamespace\nfrom mobile_sync149 import LocalFirst149\n"
            +"qa_original=m._APP\nqa_network_calls=[]\n"
            +"def qa_deny(*args,**kwargs):\n qa_network_calls.append(True)\n raise AssertionError('No fixture network allowed')\n"
            +"qa_descriptor={'authorized':True,'actor_id':991,'role_code':'member','endpoint':'https://qa.invalid/aliyun','epoch':'synthetic11828','server_instance_id':'test-server11828'}\n"
            +"qa_root=tempfile.mkdtemp(prefix='routine11828-',dir=qa_original.root)\n"
            +"qa_local=LocalFirst149(qa_root,qa_original._vault,lambda:qa_descriptor,qa_deny,qa_deny)\n"
            +"qa_identity=qa_local.identity()\n"
            +"qa_local.store.put(qa_identity,'resource','life_records',{'version':1,'value':[]})\n"
            +"qa_local.store.put(qa_identity,'resource','reminders',{'version':1,'value':[]})\n"
            +"qa_local.store.put(qa_identity,'resource','goals',{'version':1,'value':{'items':[]}})\n"
            +"m._APP=SimpleNamespace(local=qa_local,guest=None)\n";
        boolean fixture=false;long due=0,postedAt=0;JSONObject occurrence=null;String scope="";long generation=-1;
        try {
            builtins.callAttr("exec",script,namespace);fixture=true;
            LocalReminders149.cancelAll(app);LocalReminders149.refresh(app,api);
            String key=LocalReminders149.key(991,"routine11828.goals");
            occurrence=new JSONObject(LocalReminders149.registry(app).getString(key,"{}"));
            due=occurrence.getLong("at");long wait=due-System.currentTimeMillis();
            assertTrue("Prepare the owned emulator clock within90 seconds before local09:00; actual wait="+wait,wait>=0&&wait<=90000);
            scope=occurrence.getString("scope");generation=occurrence.getLong("generation");
            LocalReminders149.refresh(app,api);
            assertEquals("An unchanged foreground refresh must not rearm the occurrence",occurrence.toString(),LocalReminders149.registry(app).getString(key,"{}"));
            int planned=0;for(String name:LocalReminders149.registry(app).getAll().keySet())if(name.startsWith("alarm."))planned++;
            assertEquals("Every independent daily check is scheduled",4,planned);
            long deadline=SystemClock.elapsedRealtime()+wait+18000;StatusBarNotification actual=null;
            while(actual==null&&SystemClock.elapsedRealtime()<deadline) {
                for(StatusBarNotification row:notifications.getActiveNotifications())
                    if("life149:991:routine11828.goals".equals(row.getTag()))actual=row;
                if(actual==null)SystemClock.sleep(80);
            }
            assertNotNull("The production receiver and Python check must post the native daily reminder",actual);
            postedAt=System.currentTimeMillis();assertTrue(postedAt>=due);
            SystemClock.sleep(500); // NotificationManager posting precedes the SystemUI banner frame.
            Bitmap screenshot=InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();
            File image=new File(app.getExternalFilesDir(null),"routine-heads-up11828.png");
            if(screenshot!=null)try(FileOutputStream file=new FileOutputStream(image)){screenshot.compress(Bitmap.CompressFormat.PNG,100,file);file.flush();}
            Notification note=actual.getNotification();assertEquals("目标与习惯提醒",note.extras.getString(Notification.EXTRA_TITLE));
            assertEquals("看看今天的目标与习惯，按自己的节奏安排这一天。",note.extras.getString(Notification.EXTRA_TEXT));
            assertEquals(Notification.PRIORITY_HIGH,note.priority);assertNull(note.fullScreenIntent);
            String slotText=occurrence.getJSONObject("slot").toString();
            assertFalse("The same local day cannot be posted twice",LocalReminders149.postCurrent(app,991,"routine11828.goals",due,scope,generation,slotText,"重复提醒","不应显示"));
            assertFalse("An old logout generation cannot publish",LocalReminders149.postCurrent(app,991,"routine11828.goals",due,scope,generation-1,slotText,"旧提醒","不应显示"));
            long replanningDeadline=SystemClock.elapsedRealtime()+6000;
            while(SystemClock.elapsedRealtime()<replanningDeadline) {
                String saved=LocalReminders149.registry(app).getString(key,"{}");
                if(new JSONObject(saved).optLong("at")>due)break;
                SystemClock.sleep(80);
            }
            JSONObject next=new JSONObject(LocalReminders149.registry(app).getString(key,"{}"));
            assertTrue("Consumption must schedule the following local day",next.getLong("at")>due);
            assertNotEquals(occurrence.getJSONObject("slot").getString("day"),next.getJSONObject("slot").getString("day"));
            // Recreate the same due receipt in this isolated fixture so this
            // checks the daily stamp, rather than merely the newer alarm date.
            LocalReminders149.registry(app).edit().putString(key,occurrence.toString()).commit();
            try {
                assertFalse("The daily stamp rejects a valid repeated occurrence",LocalReminders149.postCurrent(app,991,"routine11828.goals",due,scope,generation,slotText,"重复提醒","不应显示"));
            }finally{LocalReminders149.registry(app).edit().putString(key,next.toString()).commit();}
            assertEquals("No cloud call is part of scheduling or delivery",0,builtins.callAttr("eval","len(qa_network_calls)",namespace).toInt());
            builtins.callAttr("exec","m._APP=SimpleNamespace(local=None,guest=None)",namespace);
            LocalReminders149.refresh(app,api);
            assertFalse("Guest mode must have no scheduled checks",LocalReminders149.hasScheduled(app));
            long cancellationStarted=SystemClock.elapsedRealtime(),cancellationDeadline=cancellationStarted+2000;
            int remainingNotices=notifications.getActiveNotifications().length;
            while(remainingNotices!=0&&SystemClock.elapsedRealtime()<cancellationDeadline) {
                SystemClock.sleep(20);remainingNotices=notifications.getActiveNotifications().length;
            }
            long cancellationWait=SystemClock.elapsedRealtime()-cancellationStarted;
            assertEquals("Logout/guest also removes already posted private reminders",0,remainingNotices);
            JSONObject evidence=new JSONObject().put("actual_production_receiver",true).put("actual_python_routine_check",true)
                .put("encrypted_local_fixture",true).put("synthetic_actor",991).put("real_member_account_tested",false)
                .put("network_calls",0).put("planned_daily_slots",planned).put("routine_kind","goals")
                .put("due_at",due).put("notification_observed_at",postedAt).put("notification_posted",true)
                .put("following_day_replanned",true).put("daily_duplicate_rejected",true).put("stale_generation_rejected",true)
                .put("guest_cancelled_all",true).put("system_banner_text_observed",false)
                .put("automatic_banner_text_check_performed",false).put("unchanged_refresh_kept_receipt",true)
                .put("notification_cancellation_wait_ms",cancellationWait).put("notification_cancellation_timeout_ms",2000)
                .put("system_ui_settle_ms",500)
                .put("screenshot_file",image.getName()).put("notification_channel",LocalReminders149.channelId(app));
            try(FileOutputStream file=new FileOutputStream(new File(app.getExternalFilesDir(null),"routine-native11828.json"))) {
                file.write(evidence.toString(2).getBytes(StandardCharsets.UTF_8));file.flush();
            }
        }finally {
            LocalReminders149.cancelAll(app);
            if(fixture)builtins.callAttr("exec","m._APP=qa_original\nqa_local.store.close()\nshutil.rmtree(qa_root)\n",namespace);
        }
    }
}

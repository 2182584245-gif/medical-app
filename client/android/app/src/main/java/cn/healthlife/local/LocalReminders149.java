package cn.healthlife.local;

import android.Manifest;
import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.os.Build;
import com.chaquo.python.PyObject;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.TimeZone;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONArray;
import org.json.JSONObject;

final class LocalReminders149 {
    private static final String LEGACY_CHANNEL="fawnsee-life149";
    private static final String HEADS_UP_CHANNEL="fawnsee-life-headsup11827";
    static final String CHANGED_ACTION=BuildConfig.APPLICATION_ID+".REMINDER_STATUS11827";
    private static final ExecutorService executor=Executors.newSingleThreadExecutor();
    private LocalReminders149() { }
    static SharedPreferences registry(Context c) { return c.getSharedPreferences("local-reminders149",Context.MODE_PRIVATE); }
    static String key(long actor,String id) { return "alarm."+actor+":"+id; }
    static PendingIntent intent(Context context,long actor,String id,long at) {
        return intent(context,actor,id,at,"",registry(context).getLong("generation11828",0),"");
    }
    private static PendingIntent intent(Context context,long actor,String id,long at,String scope,long generation,String slot) {
        Intent value=new Intent(context,LocalReminderReceiver149.class)
            .setAction(BuildConfig.APPLICATION_ID+".REMINDER149")
            .setData(Uri.parse("fawnsee://local-reminder/"+actor+"/"+Uri.encode(id)))
            .putExtra("actor",actor).putExtra("reminder",id).putExtra("at",at)
            .putExtra("scope11828",scope).putExtra("generation11828",generation).putExtra("slot11828",slot);
        return PendingIntent.getBroadcast(context,0,value,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
    }
    static boolean notifications(Context context) {
        NotificationManager manager=context.getSystemService(NotificationManager.class);
        if(manager==null || !manager.areNotificationsEnabled()
                || Build.VERSION.SDK_INT>=33 && context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)return false;
        NotificationChannel channel=Build.VERSION.SDK_INT>=26?manager.getNotificationChannel(channelId(context)):null;
        return channel==null || channel.getImportance()!=NotificationManager.IMPORTANCE_NONE;
    }
    static String channelId(Context context) {
        if(Build.VERSION.SDK_INT<26)return HEADS_UP_CHANNEL;
        NotificationManager manager=context.getSystemService(NotificationManager.class);
        if(manager==null)return HEADS_UP_CHANNEL;
        NotificationChannel current=manager.getNotificationChannel(HEADS_UP_CHANNEL);
        if(current!=null)return HEADS_UP_CHANNEL;
        NotificationChannel legacy=manager.getNotificationChannel(LEGACY_CHANNEL);
        if(legacy!=null && (legacy.canBypassDnd() || !NativeReminderDeliveryPolicy11827.migrateDefaultChannel(Build.VERSION.SDK_INT,
                legacy.getImportance(),Build.VERSION.SDK_INT>=29&&legacy.hasUserSetImportance())))return LEGACY_CHANNEL;
        NotificationChannel channel=new NotificationChannel(HEADS_UP_CHANNEL,"生活提醒（到时通知）",NotificationManager.IMPORTANCE_HIGH);
        channel.setDescription("在你设定的时刻提醒，可在手机通知设置中选择横幅、声音和振动。");
        if(legacy==null) {
            channel.setLockscreenVisibility(Notification.VISIBILITY_PRIVATE);
            channel.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION),
                new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION).build());
            channel.enableVibration(true);
            channel.setVibrationPattern(new long[]{0,180,90,180});
        } else {
            // Upgrade only the untouched importance. Copy every accessible
            // presentation preference instead of recreating user sound choices.
            channel.setSound(legacy.getSound(),legacy.getAudioAttributes());
            if(legacy.getVibrationPattern()!=null)channel.setVibrationPattern(legacy.getVibrationPattern());
            channel.enableVibration(legacy.shouldVibrate());
            channel.enableLights(legacy.shouldShowLights());channel.setLightColor(legacy.getLightColor());
            channel.setShowBadge(legacy.canShowBadge());channel.setLockscreenVisibility(legacy.getLockscreenVisibility());
        }
        manager.createNotificationChannel(channel);
        return HEADS_UP_CHANNEL;
    }
    static boolean headsUpEligible(Context context) {
        if(!notifications(context))return false;
        if(Build.VERSION.SDK_INT<26)return true;
        NotificationManager manager=context.getSystemService(NotificationManager.class);
        NotificationChannel channel=manager==null?null:manager.getNotificationChannel(channelId(context));
        return channel!=null && channel.getImportance()>=NotificationManager.IMPORTANCE_HIGH;
    }
    static boolean arm(AlarmManager alarms,long at,PendingIntent pending) {
        boolean exact=Build.VERSION.SDK_INT<31 || alarms.canScheduleExactAlarms();
        try {
            if(exact)alarms.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP,at,pending);
            else alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP,at,pending);
        }catch(SecurityException ignored){exact=false;alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP,at,pending);}
        return exact;
    }
    static Notification notice(Context context,String title) {
        return notice(context,title,"到时间了，打开凡思查看今天的安排。");
    }
    static Notification notice(Context context,String title,String body) {
        PendingIntent open=PendingIntent.getActivity(context,0,new Intent(context,MainActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP|Intent.FLAG_ACTIVITY_SINGLE_TOP),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder=Build.VERSION.SDK_INT>=26?new Notification.Builder(context,channelId(context)):new Notification.Builder(context);
        builder.setSmallIcon(android.R.drawable.ic_popup_reminder).setContentTitle(title)
            .setContentText(body).setStyle(new Notification.BigTextStyle().bigText(body))
            .setContentIntent(open).setAutoCancel(true).setVisibility(Notification.VISIBILITY_PRIVATE)
            .setCategory(Notification.CATEGORY_REMINDER).setPriority(Notification.PRIORITY_HIGH);
        Notification.Builder publicBuilder=Build.VERSION.SDK_INT>=26?new Notification.Builder(context,channelId(context)):new Notification.Builder(context);
        builder.setPublicVersion(publicBuilder.setSmallIcon(android.R.drawable.ic_popup_reminder)
            .setContentTitle("凡思生活提醒").setContentText("有一条生活提醒，解锁后查看。")
            .setVisibility(Notification.VISIBILITY_PUBLIC).setContentIntent(open).setAutoCancel(true)
            .setCategory(Notification.CATEGORY_REMINDER).build());
        if(Build.VERSION.SDK_INT<26)builder.setDefaults(Notification.DEFAULT_SOUND|Notification.DEFAULT_VIBRATE);
        return builder.build();
    }
    static boolean hasScheduled(Context context) {
        for(String name:registry(context).getAll().keySet())if(name.startsWith("alarm."))return true;
        return false;
    }
    static JSONObject capabilities(Context context) throws Exception {
        AlarmManager alarms=context.getSystemService(AlarmManager.class);
        return new JSONObject().put("notifications_enabled",notifications(context))
            .put("heads_up_eligible",headsUpEligible(context)).put("notification_channel_id",channelId(context))
            .put("system_notification_settings",true).put("lockscreen_content","private_with_public_preview")
            .put("scheduled_reminders_present",hasScheduled(context))
            .put("exact_alarms_enabled",Build.VERSION.SDK_INT<31 || alarms!=null && alarms.canScheduleExactAlarms());
    }
    static void refreshAsync(Context context) {
        Context app=context.getApplicationContext();
        executor.execute(()-> {try {
            refresh(app,MobileRuntime149.module(app));
            app.sendBroadcast(new Intent(CHANGED_ACTION).setPackage(app.getPackageName()));
        }catch(Exception ignored) { }});
    }
    static synchronized void refresh(Context context,PyObject api) throws Exception {
        long now=System.currentTimeMillis();TimeZone zone=TimeZone.getDefault();JSONArray slots=new JSONArray();
        for(NativeRoutinePolicy11828.Slot slot:NativeRoutinePolicy11828.next(now,zone))slots.put(encode(slot));
        JSONObject envelope=new JSONObject(api.callAttr("local_reminders",slots.toString()).toString());
        if (!envelope.optBoolean("ok")) return;
        JSONObject result=envelope.getJSONObject("result");
        long actor=result.optLong("actor_id",0);
        String scope=result.optString("scope","");
        JSONArray items=result.optJSONArray("items");
        if (actor<=0 || items==null) {cancelAll(context);return;}
        // Retain the existing512 custom reminders plus the four daily checks.
        if (items.length()>516) throw new IllegalArgumentException("Reminder snapshot too large");
        AlarmManager alarms=context.getSystemService(AlarmManager.class);
        if (alarms==null) return;
        SharedPreferences store=registry(context);Set<String> wanted=new HashSet<>();
        if(store.getLong("actor11828",0)!=actor||!store.getString("scope11828","").equals(scope))cancelAll(context);
        long generation=store.getLong("generation11828",0);
        store.edit().putLong("actor11828",actor).putString("scope11828",scope).apply();
        for(int i=0;i<items.length();i++) {
            JSONObject item=items.getJSONObject(i);String id=item.getString("id");long at=item.getLong("at");
            boolean routine=NativeRoutinePolicy11828.routine(id);
            NativeRoutinePolicy11828.Slot slot=routine?decode(item):null;
            if(routine) {
                if(!NativeRoutinePolicy11828.validScope(scope)||!NativeRoutinePolicy11828.valid(slot,now,zone,false)
                        ||at<now-60000||at>now+27*60*60*1000L)continue;
            }else if(!id.matches("-?[0-9]{1,16}|[a-fA-F0-9-]{36}")||at<now-60000)continue;
            String name=key(actor,id);wanted.add(name);
            boolean exact=Build.VERSION.SDK_INT<31 || alarms.canScheduleExactAlarms();
            String saved=store.getString(name,null);
            if(saved!=null)try {
                JSONObject previous=new JSONObject(saved);
                // Preserve a due check while its broadcast is queued behind a
                // simultaneous foreground refresh. Consumption schedules tomorrow.
                if(routine&&NativeRoutinePolicy11828.currentReceipt(actor,scope,generation,previous.optLong("actor"),
                        previous.optString("scope"),previous.optLong("generation",-1),previous.optLong("at"),previous.optLong("at"))) {
                    NativeRoutinePolicy11828.Slot due=decode(previous.optJSONObject("slot"));
                    if(NativeRoutinePolicy11828.valid(due,now,zone,true)) {slot=due;at=due.at;}
                }
                if(NativeReminderDeliveryPolicy11827.sameReceipt(previous.optLong("actor"),previous.optString("id"),
                        previous.optLong("at"),previous.optString("mode"),actor,id,at,exact)
                        &&scope.equals(previous.optString("scope"))&&generation==previous.optLong("generation",-1)
                        &&(!routine||NativeRoutinePolicy11828.same(slot,decode(previous.optJSONObject("slot")))))continue;
            }catch(Exception ignored) { }
            String slotText=routine?encode(slot).toString():"";
            PendingIntent pending=intent(context,actor,id,at,scope,generation,slotText);
            exact=arm(alarms,at,pending);
            String encoded=new JSONObject().put("actor",actor).put("id",id).put("at",at)
                .put("scope",scope).put("generation",generation).put("slot",routine?encode(slot):JSONObject.NULL)
                .put("mode",NativeReminderDeliveryPolicy11827.mode(exact)).toString();
            store.edit().putString(name,encoded).apply();
        }
        for(Map.Entry<String,?> row:store.getAll().entrySet()) {
            if(!row.getKey().startsWith("alarm.") || wanted.contains(row.getKey()))continue;
            cancel(context,row.getKey(),String.valueOf(row.getValue()));
        }
    }
    private static void cancel(Context context,String name,String encoded) {
        try {
            JSONObject item=new JSONObject(encoded);
            PendingIntent pending=intent(context,item.getLong("actor"),item.getString("id"),item.getLong("at"),
                item.optString("scope"),item.optLong("generation",-1),item.optJSONObject("slot")==null?"":item.getJSONObject("slot").toString());
            AlarmManager manager=context.getSystemService(AlarmManager.class);if(manager!=null)manager.cancel(pending);
            pending.cancel();
        }catch(Exception ignored) { }
        registry(context).edit().remove(name).apply();
    }
    static synchronized void cancelAll(Context context) {
        SharedPreferences store=registry(context);
        long generation=store.getLong("generation11828",0);
        store.edit().putLong("generation11828",generation==Long.MAX_VALUE?0:generation+1)
            .remove("actor11828").remove("scope11828").apply();
        for(Map.Entry<String,?> row:registry(context).getAll().entrySet())
            if(row.getKey().startsWith("alarm."))cancel(context,row.getKey(),String.valueOf(row.getValue()));
        NotificationManager manager=context.getSystemService(NotificationManager.class);
        if(manager!=null)manager.cancelAll();
    }
    private static synchronized void consumed(Context context,long actor,String id,long at,String scope,long generation) throws Exception {
        // AlarmManager consumes a one-shot PendingIntent even when showing the
        // notice was blocked. Retire only this occurrence: a stale broadcast
        // must never erase an edited reminder's newer scheduling receipt.
        String name=key(actor,id),encoded=registry(context).getString(name,null);
        if(encoded!=null) {
            JSONObject current=new JSONObject(encoded);
            if(matches(context,current,actor,id,at,scope,generation))registry(context).edit().remove(name).apply();
        }
        refresh(context,MobileRuntime149.module(context));
    }
    static JSONObject encode(NativeRoutinePolicy11828.Slot slot) throws Exception {
        return new JSONObject().put("id",slot.id).put("kind",slot.kind).put("day",slot.day).put("at",slot.at)
            .put("day_start",slot.dayStart).put("day_end",slot.dayEnd)
            .put("previous_day_start",slot.previousDayStart).put("previous_day_end",slot.previousDayEnd);
    }
    static NativeRoutinePolicy11828.Slot decode(JSONObject slot) throws Exception {
        if(slot==null)return null;
        NativeRoutinePolicy11828.Slot value=new NativeRoutinePolicy11828.Slot(slot.getString("kind"),slot.getString("day"),
            slot.getLong("at"),slot.getLong("day_start"),slot.getLong("day_end"),slot.getLong("previous_day_start"),slot.getLong("previous_day_end"));
        if(!value.id.equals(slot.getString("id")))throw new IllegalArgumentException("Routine identifier mismatch");
        return value;
    }
    private static boolean matches(Context context,JSONObject row,long actor,String id,long at,String scope,long generation) {
        boolean occurrence=row!=null&&actor>0&&actor==row.optLong("actor")&&id!=null&&id.equals(row.optString("id"))&&at>0&&at==row.optLong("at");
        // Pre-upgrade ordinary reminders have neither a scope nor generation.
        // Their established Python account/date validation remains unchanged.
        if(!NativeRoutinePolicy11828.routine(id))return occurrence;
        return occurrence&&scope!=null&&scope.equals(row.optString("scope"))
            &&generation>=0&&generation==row.optLong("generation",-1)
            &&generation==registry(context).getLong("generation11828",0);
    }
    private static synchronized boolean current(Context app,long actor,String id,long at,String scope,long generation,String slotText) throws Exception {
        String saved=registry(app).getString(key(actor,id),null);
        if(saved==null||!matches(app,new JSONObject(saved),actor,id,at,scope,generation))return false;
        if(NativeRoutinePolicy11828.routine(id)) {
            NativeRoutinePolicy11828.Slot slot=decode(new JSONObject(slotText));
            return NativeRoutinePolicy11828.valid(slot,System.currentTimeMillis(),TimeZone.getDefault(),true)
                &&NativeRoutinePolicy11828.same(slot,decode(new JSONObject(saved).optJSONObject("slot")));
        }
        return true;
    }
    static synchronized boolean postCurrent(Context app,long actor,String id,long at,String scope,long generation,String slotText,String title,String text) throws Exception {
        if(title==null||title.isEmpty()||title.length()>200||text==null||text.isEmpty()||text.length()>2000
                ||!current(app,actor,id,at,scope,generation,slotText)||!notifications(app))return false;
        String stamp=NativeRoutinePolicy11828.routine(id)?NativeRoutinePolicy11828.dailyStamp(scope,decode(new JSONObject(slotText))):"shown."+actor+":"+id+":"+at;
        if(registry(app).getBoolean(stamp,false))return false;
        NotificationManager manager=app.getSystemService(NotificationManager.class);if(manager==null)return false;
        manager.notify("life149:"+actor+":"+id,149,notice(app,title,text));
        registry(app).edit().putBoolean(stamp,true).apply();
        if(registry(app).getAll().size()>1536) {
            SharedPreferences.Editor edit=registry(app).edit();
            String oldest=NativeRoutinePolicy11828.next(System.currentTimeMillis()-7*24*60*60*1000L,TimeZone.getDefault()).get(0).day;
            for(String name:registry(app).getAll().keySet())if(name.startsWith("shown.")&&!name.equals(stamp)) {
                if(!name.startsWith("shown.routine11828.")||name.substring(name.lastIndexOf(':')+1).compareTo(oldest)<0)edit.remove(name);
            }
            edit.apply();
        }
        return true;
    }
    static void fired(Context context,long actor,String id,long at,String scope,long generation,String slotText,Runnable completion) {
        Context app=context.getApplicationContext();
        executor.execute(()-> {
            try {
                if(actor<=0||id==null||at<=0||!notifications(app)||!current(app,actor,id,at,scope,generation,slotText))return;
                PyObject api=MobileRuntime149.module(app);
                boolean routine=NativeRoutinePolicy11828.routine(id);
                JSONObject reply=new JSONObject((routine?api.callAttr("routine_fired11828",actor,scope,slotText,System.currentTimeMillis()):
                    api.callAttr("reminder_fired",actor,id,at)).toString());
                JSONObject body=reply.optJSONObject("result");
                if(!reply.optBoolean("ok") || body==null || !body.optBoolean("show") || body.optLong("actor_id")!=actor)return;
                if(routine&&!scope.equals(body.optString("scope")))return;
                postCurrent(app,actor,id,at,scope,generation,slotText,body.getString("title"),
                    routine?body.getString("body"):"到时间了，打开凡思查看今天的安排。");
            }catch(Exception ignored) { }
            finally {
                try {
                    if(actor>0 && id!=null && at>0)consumed(app,actor,id,at,scope,generation);
                }catch(Exception ignored) { }
                finally {completion.run();}
            }
        });
    }
    static void fired(Context context,long actor,String id,long at,Runnable completion) {
        fired(context,actor,id,at,"",-1,"",completion);
    }
}

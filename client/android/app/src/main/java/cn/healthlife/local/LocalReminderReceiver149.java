package cn.healthlife.local;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public final class LocalReminderReceiver149 extends BroadcastReceiver {
    @Override public void onReceive(Context context,Intent intent) {
        if(intent==null || !(BuildConfig.APPLICATION_ID+".REMINDER149").equals(intent.getAction()))return;
        PendingResult work=goAsync();
        LocalReminders149.fired(context,intent.getLongExtra("actor",0),intent.getStringExtra("reminder"),intent.getLongExtra("at",0),
            intent.getStringExtra("scope11828"),intent.getLongExtra("generation11828",-1),intent.getStringExtra("slot11828"),work::finish);
    }
}

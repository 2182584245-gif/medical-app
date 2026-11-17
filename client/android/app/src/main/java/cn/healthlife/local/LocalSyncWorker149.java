package cn.healthlife.local;

import android.content.Context;
import androidx.annotation.NonNull;
import androidx.work.Worker;
import androidx.work.WorkerParameters;
import com.chaquo.python.PyObject;
import org.json.JSONObject;

public final class LocalSyncWorker149 extends Worker {
    public LocalSyncWorker149(@NonNull Context context,@NonNull WorkerParameters parameters) { super(context,parameters); }
    @NonNull @Override public Result doWork() {
        try {
            PyObject api=MobileRuntime149.module(getApplicationContext());
            JSONObject result=new JSONObject(api.callAttr("sync_background").toString());
            LocalReminders149.refresh(getApplicationContext(),api);
            new NativeLocalHooks149(getApplicationContext()).syncFinished(result.toString());
            String code=result.optJSONObject("error")==null?"":result.getJSONObject("error").optString("code");
            if (result.optBoolean("ok")) {
                JSONObject body=result.optJSONObject("result");
                return body!=null && body.optBoolean("more_pending")?Result.retry():Result.success();
            }
            if (code.equals("not_authenticated") || code.equals("online_login_required") || code.equals("offline_expired")) return Result.success();
            return getRunAttemptCount()<6?Result.retry():Result.failure();
        } catch (Exception ignored) { return getRunAttemptCount()<6?Result.retry():Result.failure(); }
    }
}

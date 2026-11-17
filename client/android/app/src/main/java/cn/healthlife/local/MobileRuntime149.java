package cn.healthlife.local;

import android.content.Context;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import android.util.Log;
import java.io.File;
import org.json.JSONObject;

final class MobileRuntime149 {
    private static PyObject module;
    private MobileRuntime149() { }
    static synchronized PyObject module(Context context) throws Exception {
        if (module == null) {
            Context app=context.getApplicationContext();
            ClientDataReset.requireReady(app);
            if (!Python.isStarted()) Python.start(new VerifiedPythonPlatform(app));
            PyObject candidate=Python.getInstance().getModule("mobile_api");
            JSONObject reply=new JSONObject(candidate.callAttr("initialize",
                new File(app.getFilesDir(),"member").getAbsolutePath(),new LocalDataVault(),new NativeLocalHooks149(app)).toString());
            if (!reply.optBoolean("ok")) throw new IllegalStateException("Local service unavailable");
            module=candidate;
            Log.i("FawnseeRuntime","ready");
        }
        return module;
    }
}

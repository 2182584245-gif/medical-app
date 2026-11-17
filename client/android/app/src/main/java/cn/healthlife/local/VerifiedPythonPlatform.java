package cn.healthlife.local;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;
import android.util.Log;
import com.chaquo.python.android.AndroidPlatform;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;
import org.json.JSONObject;

/** Retains SDK extraction; checks bootstrap bytes before native Python starts. */
final class VerifiedPythonPlatform extends AndroidPlatform {
    private final Context app;
    VerifiedPythonPlatform(Context context) {super(context);app=context.getApplicationContext();}

    @Override public String getPath() {
        try {
            JSONObject assets=new JSONObject(readBuild()).getJSONObject("assets");
            Map<String,String> manifest=new LinkedHashMap<>();
            for(Iterator<String> keys=assets.keys();keys.hasNext();) {
                String key=keys.next();manifest.put(key,assets.getString(key));
            }
            PythonBootstrapIntegrity.Selection selection=PythonBootstrapIntegrity.select(manifest,Build.SUPPORTED_ABIS);
            if(!selection.abi.equals(AndroidPlatform.ABI))throw new IOException("Python bootstrap unavailable");
            SharedPreferences preferences=app.getSharedPreferences("chaquopy",Context.MODE_PRIVATE);
            PythonBootstrapIntegrity.Result result=PythonBootstrapIntegrity.prepare(app.getFilesDir(),app.getCacheDir(),selection,
                path->app.getAssets().open("chaquopy/"+path),
                path->{
                    try {return OsConstants.S_ISLNK(Os.lstat(path.getPath()).st_mode);}
                    catch(ErrnoException error) {
                        if(error.errno==OsConstants.ENOENT)return false;
                        throw new IOException("Python bootstrap unavailable");
                    }
                },
                keys->{
                    SharedPreferences.Editor editor=preferences.edit();
                    for(String key:keys)editor.remove(key);
                    return editor.commit();
                },()->super.getPath());
            if(result.repaired)Log.i("FawnseeRuntime","bootstrap-repaired");
            return result.path;
        } catch(Exception error) {
            // Deliberately omit paths, preference values and exception bodies.
            throw new IllegalStateException("Python bootstrap unavailable");
        }
    }
    private String readBuild() throws IOException {
        try(InputStream stream=app.getAssets().open("chaquopy/build.json");ByteArrayOutputStream bytes=new ByteArrayOutputStream()) {
            byte[] buffer=new byte[4096];int count;
            while((count=stream.read(buffer))!=-1) {
                if(bytes.size()+count>1024*1024)throw new IOException("Python bootstrap unavailable");
                bytes.write(buffer,0,count);
            }
            return new String(bytes.toByteArray(),StandardCharsets.UTF_8);
        }
    }
}

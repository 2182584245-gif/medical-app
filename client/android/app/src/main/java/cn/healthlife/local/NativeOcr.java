package cn.healthlife.local;

import android.graphics.Bitmap;
import com.google.android.gms.tasks.Tasks;
import com.google.android.gms.tasks.Task;
import com.google.mlkit.vision.common.InputImage;
import com.google.mlkit.vision.text.TextRecognizer;
import com.google.mlkit.vision.text.Text;
import com.google.mlkit.vision.text.TextRecognition;
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions;
import java.io.File;
import java.util.concurrent.TimeUnit;

/** Bundled, offline OCR. Called only on the Python worker, never the UI thread. */
public final class NativeOcr {
    private NativeOcr() {}
    public static String recognize(String path) throws Exception {
        File file = new File(path);
        if (!file.isFile() || file.length() > 15L * 1024 * 1024) {
            throw new IllegalArgumentException("请选择不超过 15 MB 的图片");
        }
        Bitmap bitmap = BoundedImage.decode(path, 4096);
        TextRecognizer recognizer = TextRecognition.getClient(new ChineseTextRecognizerOptions.Builder().build());
        Task<Text> task;
        try {
            task = recognizer.process(InputImage.fromBitmap(bitmap, 0));
        } catch (RuntimeException error) {
            recognizer.close();
            bitmap.recycle();
            throw error;
        }
        // A timeout of await does NOT cancel ML Kit: retain input until its task finishes.
        task.addOnCompleteListener(Runnable::run, finished -> { recognizer.close(); bitmap.recycle(); });
        return Tasks.await(task, 90, TimeUnit.SECONDS).getText();
    }
}

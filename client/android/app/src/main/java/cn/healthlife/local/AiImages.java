package cn.healthlife.local;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.util.Base64;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.File;

/** A fixed AI-proxy image boundary. Forum photos keep their separate original-format path. */
final class AiImages {
    static final int MAX_EDGE = 2048;
    private AiImages() {}

    static JSONObject fromBytes(byte[] bytes, String baseName) throws Exception {
        String encoded = Base64.encodeToString(bytes, Base64.NO_WRAP);
        JSONObject checked = new JSONObject(NativeImages.validate(encoded, "", 8192, 20_000_000));
        if (!checked.optBoolean("ok")) throw new IllegalArgumentException();
        String mime = checked.getString("mime");
        if ((mime.equals("image/jpeg") || mime.equals("image/png"))
                && checked.getInt("width") <= MAX_EDGE && checked.getInt("height") <= MAX_EDGE) {
            return result(bytes, baseName, mime);
        }
        return encode(BoundedImage.decode(bytes, MAX_EDGE), baseName);
    }

    static JSONObject fromCamera(File file) throws Exception {
        if (file == null || !file.isFile() || file.length() > 40L * 1024 * 1024) throw new IllegalArgumentException();
        return encode(BoundedImage.decode(file.getAbsolutePath(), MAX_EDGE), "FAWNSEE-photo");
    }

    private static JSONObject encode(Bitmap decoded, String baseName) throws Exception {
        Bitmap flattened = null;
        try {
            if (decoded.getWidth() > MAX_EDGE || decoded.getHeight() > MAX_EDGE) throw new IllegalArgumentException();
            Bitmap input = decoded;
            if (decoded.hasAlpha()) {
                flattened = Bitmap.createBitmap(decoded.getWidth(), decoded.getHeight(), Bitmap.Config.ARGB_8888);
                Canvas canvas = new Canvas(flattened); canvas.drawColor(Color.WHITE); canvas.drawBitmap(decoded, 0, 0, null);
                input = flattened;
            }
            byte[] payload = null;
            for (int quality : new int[]{92, 85, 75, 60, 45}) {
                ByteArrayOutputStream output = new ByteArrayOutputStream();
                if (!input.compress(Bitmap.CompressFormat.JPEG, quality, output)) throw new IllegalArgumentException();
                payload = output.toByteArray();
                if (payload.length <= MainActivity.IMAGE_LIMIT) break;
            }
            if (payload == null || payload.length > MainActivity.IMAGE_LIMIT) throw new IllegalArgumentException();
            JSONObject checked = new JSONObject(NativeImages.validate(Base64.encodeToString(payload, Base64.NO_WRAP), "image/jpeg", MAX_EDGE, (long) MAX_EDGE * MAX_EDGE));
            if (!checked.optBoolean("ok")) throw new IllegalArgumentException();
            return result(payload, baseName, "image/jpeg");
        } finally {
            if (flattened != null) flattened.recycle();
            decoded.recycle();
        }
    }

    private static JSONObject result(byte[] bytes, String baseName, String mime) throws Exception {
        return new JSONObject().put("name", baseName + (mime.equals("image/png") ? ".png" : ".jpg"))
            .put("media_type", mime).put("data_base64", Base64.encodeToString(bytes, Base64.NO_WRAP)).put("size_bytes", bytes.length);
    }
}

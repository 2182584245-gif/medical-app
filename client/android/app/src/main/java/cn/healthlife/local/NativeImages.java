package cn.healthlife.local;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.util.Base64;
import org.json.JSONObject;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.zip.CRC32;
import java.io.ByteArrayOutputStream;

/** Complete bounded image decoding using the device's maintained Android codecs, without bundled legacy ELF libraries. */
public final class NativeImages {
    private NativeImages() {}
    /** Transport-only copy for the model's PNG/JPEG contract; never edits the source file. */
    public static String toJpeg(String encoded) {
        Bitmap source=null, flattened=null;
        final int limit=2*1024*1024;
        try {
            if(encoded==null || encoded.length()>3*1024*1024)throw new IllegalArgumentException();
            byte[] raw=Base64.decode(encoded,Base64.DEFAULT);
            if(raw.length<20 || raw.length>limit || !MainActivity.imageType(raw).equals("image/webp")
                    || (ByteBuffer.wrap(raw,4,4).order(ByteOrder.LITTLE_ENDIAN).getInt()&0xffffffffL)+8!=raw.length)
                throw new IllegalArgumentException();
            BitmapFactory.Options bounds=new BitmapFactory.Options();bounds.inJustDecodeBounds=true;
            BitmapFactory.decodeByteArray(raw,0,raw.length,bounds);
            if(bounds.outWidth<1 || bounds.outHeight<1 || bounds.outWidth>12000 || bounds.outHeight>12000
                    || (long)bounds.outWidth*bounds.outHeight>20_000_000L)throw new IllegalArgumentException();
            source=BitmapFactory.decodeByteArray(raw,0,raw.length);
            if(source==null || source.getWidth()!=bounds.outWidth || source.getHeight()!=bounds.outHeight)throw new IllegalArgumentException();
            Bitmap picture=source;
            if(source.hasAlpha()) {
                flattened=Bitmap.createBitmap(source.getWidth(),source.getHeight(),Bitmap.Config.ARGB_8888);
                Canvas canvas=new Canvas(flattened);canvas.drawColor(Color.WHITE);canvas.drawBitmap(source,0,0,null);picture=flattened;
            }
            ByteArrayOutputStream output=new ByteArrayOutputStream() {
                @Override public synchronized void write(int value) {if(count>=limit)throw new IllegalArgumentException();super.write(value);}
                @Override public synchronized void write(byte[] bytes,int offset,int length) {if(length<0 || count>limit-length)throw new IllegalArgumentException();super.write(bytes,offset,length);}
            };
            if(!picture.compress(Bitmap.CompressFormat.JPEG,95,output) || output.size()<4 || output.size()>limit)throw new IllegalArgumentException();
            return new JSONObject().put("ok",true).put("mime","image/jpeg").put("width",bounds.outWidth).put("height",bounds.outHeight)
                .put("data_base64",Base64.encodeToString(output.toByteArray(),Base64.NO_WRAP)).toString();
        } catch(Exception|OutOfMemoryError ignored) {return "{\"ok\":false,\"error\":\"invalid_image\"}";}
        finally {if(flattened!=null)flattened.recycle();if(source!=null)source.recycle();}
    }
    public static String validate(String encoded, String expectedMime, int maxEdge, long maxPixels) {
        Bitmap bitmap = null;
        try {
            if (encoded == null || encoded.length() > 3 * 1024 * 1024 || maxEdge < 1 || maxPixels < 1) throw new IllegalArgumentException();
            byte[] bytes = Base64.decode(encoded, Base64.DEFAULT);
            String mime = MainActivity.imageType(bytes);
            if (!expectedMime.isEmpty() && !mime.equals(expectedMime)) throw new IllegalArgumentException();
            if (mime.equals("image/png")) validatePng(bytes);
            if (mime.equals("image/jpeg") && ((bytes[bytes.length - 2] & 255) != 255 || (bytes[bytes.length - 1] & 255) != 217)) throw new IllegalArgumentException();
            if (mime.equals("image/webp") && (ByteBuffer.wrap(bytes, 4, 4).order(ByteOrder.LITTLE_ENDIAN).getInt() & 0xffffffffL) + 8 != bytes.length) throw new IllegalArgumentException();
            BitmapFactory.Options bounds = new BitmapFactory.Options(); bounds.inJustDecodeBounds = true;
            BitmapFactory.decodeByteArray(bytes, 0, bytes.length, bounds);
            if (bounds.outWidth < 1 || bounds.outHeight < 1 || bounds.outWidth > maxEdge || bounds.outHeight > maxEdge
                    || (long) bounds.outWidth * bounds.outHeight > maxPixels) throw new IllegalArgumentException();
            bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.length);
            if (bitmap == null || bitmap.getWidth() != bounds.outWidth || bitmap.getHeight() != bounds.outHeight) throw new IllegalArgumentException();
            return new JSONObject().put("ok", true).put("mime", mime).put("width", bounds.outWidth).put("height", bounds.outHeight).toString();
        } catch (Exception | OutOfMemoryError ignored) {
            return "{\"ok\":false,\"error\":\"invalid_image\"}";
        } finally { if (bitmap != null) bitmap.recycle(); }
    }
    private static void validatePng(byte[] bytes) {
        int offset = 8; boolean end = false;
        while (offset + 12 <= bytes.length) {
            long length = ByteBuffer.wrap(bytes, offset, 4).getInt() & 0xffffffffL;
            if (length > MainActivity.IMAGE_LIMIT || offset + length + 12 > bytes.length) throw new IllegalArgumentException();
            int count = (int) length;
            CRC32 crc = new CRC32(); crc.update(bytes, offset + 4, count + 4);
            long expected = ByteBuffer.wrap(bytes, offset + count + 8, 4).getInt() & 0xffffffffL;
            if (crc.getValue() != expected) throw new IllegalArgumentException();
            boolean iend = bytes[offset + 4] == 73 && bytes[offset + 5] == 69 && bytes[offset + 6] == 78 && bytes[offset + 7] == 68;
            offset += count + 12;
            if (iend) { if (count != 0 || offset != bytes.length) throw new IllegalArgumentException(); end = true; break; }
        }
        if (!end) throw new IllegalArgumentException();
    }
}

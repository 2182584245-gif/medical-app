package cn.healthlife.local;

import android.app.Activity;
import android.app.Dialog;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.pdf.PdfRenderer;
import android.os.ParcelFileDescriptor;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.io.File;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Bounded image/PDF preview without sharing private files with other apps. */
final class FilePreview {
    private final Activity activity;
    private final File file;
    private final String mime;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final Dialog dialog;
    private final ImageView image;
    private final TextView status;
    private PdfRenderer pdf;
    private ParcelFileDescriptor descriptor;
    private Bitmap shown;
    private volatile boolean closed, busy;
    private int pageIndex;

    private FilePreview(Activity activity, File file, String mime, String name) {
        this.activity = activity; this.file = file; this.mime = mime;
        dialog = new Dialog(activity);
        LinearLayout layout = new LinearLayout(activity);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(20, 32, 20, 24);
        layout.setBackgroundColor(Color.WHITE);
        TextView title = new TextView(activity);
        title.setText(name); title.setTextSize(18);
        layout.addView(title);
        status = new TextView(activity); status.setText("正在读取预览…"); layout.addView(status);
        image = new ImageView(activity); image.setScaleType(ImageView.ScaleType.FIT_CENTER);
        layout.addView(image, new LinearLayout.LayoutParams(-1, 0, 1));
        LinearLayout buttons = new LinearLayout(activity);
        Button previous = new Button(activity); previous.setText("上一页"); previous.setOnClickListener(v -> render(-1));
        Button next = new Button(activity); next.setText("下一页"); next.setOnClickListener(v -> render(1));
        Button close = new Button(activity); close.setText("关闭"); close.setOnClickListener(v -> dialog.dismiss());
        if (mime.equals("application/pdf")) { buttons.addView(previous); buttons.addView(next); }
        buttons.addView(close); layout.addView(buttons);
        dialog.setContentView(layout);
        dialog.setOnDismissListener(ignored -> {
            closed = true;
            image.setImageDrawable(null);
            if (shown != null) { shown.recycle(); shown = null; }
            worker.execute(() -> {
                if (pdf != null) pdf.close();
                try { if (descriptor != null) descriptor.close(); } catch (Exception ignoredClose) { }
                file.delete();
            });
            worker.shutdown();
        });
    }
    static FilePreview show(Activity activity, File file, String mime, String name) {
        FilePreview preview = new FilePreview(activity, file, mime, name);
        preview.dialog.show();
        if (preview.dialog.getWindow() != null) preview.dialog.getWindow().setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        preview.render(0);
        return preview;
    }
    void close() { if (!closed) dialog.dismiss(); }
    private void render(int change) {
        if (closed || busy) return;
        busy = true;
        worker.execute(() -> {
            try {
                Bitmap bitmap;
                String label;
                if (mime.equals("application/pdf")) {
                    if (pdf == null) {
                        descriptor = ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY);
                        pdf = new PdfRenderer(descriptor);
                    }
                    if (pdf.getPageCount() == 0) throw new IllegalArgumentException();
                    pageIndex = Math.max(0, Math.min(pdf.getPageCount() - 1, pageIndex + change));
                    try (PdfRenderer.Page page = pdf.openPage(pageIndex)) {
                        float ratio = Math.min(2f, 2048f / Math.max(page.getWidth(), page.getHeight()));
                        bitmap = Bitmap.createBitmap(Math.max(1, (int)(page.getWidth()*ratio)), Math.max(1, (int)(page.getHeight()*ratio)), Bitmap.Config.ARGB_8888);
                        bitmap.eraseColor(Color.WHITE);
                        page.render(bitmap, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY);
                    }
                    label = "第 " + (pageIndex + 1) + " / " + pdf.getPageCount() + " 页 · 原始文件预览";
                } else {
                    bitmap = BoundedImage.decode(file.getAbsolutePath(), 2048);
                    label = "原始图片预览 · OCR 结果需要逐项核对";
                }
                activity.runOnUiThread(() -> {
                    busy = false;
                    if (closed || activity.isDestroyed() || activity.isFinishing()) { bitmap.recycle(); return; }
                    Bitmap old = shown; shown = bitmap; image.setImageBitmap(bitmap);
                    if (old != null) old.recycle();
                    status.setText(label);
                });
            } catch (Exception | OutOfMemoryError error) {
                activity.runOnUiThread(() -> { busy = false; if (!closed) status.setText(R.string.preview_unavailable); });
            }
        });
    }
}

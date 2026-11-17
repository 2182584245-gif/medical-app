package cn.healthlife.local;

import androidx.core.content.FileProvider;

/** A separate component keeps update and image authorities independent in Android. */
public final class UpdateFileProvider extends FileProvider {
    public UpdateFileProvider() { super(); }
}

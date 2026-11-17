package cn.healthlife.local;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/** Fixed private-directory cleanup. Never follows a link or accepts a web path. */
final class ClientResetFiles {
    static final String EPOCH="client-account-epoch-v3";
    interface LinkInspector { boolean isSymbolicLink(File value) throws IOException; }
    private ClientResetFiles() { }
    static boolean complete(File files) throws IOException {
        File marker=child(files,"client-account-epoch");
        if(!marker.exists())return false;
        if(!marker.isFile() || marker.length()>80)throw new IOException("Invalid reset marker");
        byte[] bytes=new byte[(int)marker.length()];
        try(FileInputStream input=new FileInputStream(marker)) {
            if(input.read(bytes)!=bytes.length)return false;
        }
        return EPOCH.equals(new String(bytes,StandardCharsets.US_ASCII));
    }
    static File child(File parent,String name) throws IOException {
        File value=leaf(directory(parent),name);
        if(!value.equals(value.getCanonicalFile()))throw new IOException("Linked private file");
        return value;
    }
    private static File directory(File parent) throws IOException {
        if(parent==null)throw new IOException("Invalid private directory");
        // Context roots can use system aliases (/data/user/0 and /data/data).
        // Trust that supplied root once, then validate every child separately.
        File anchor=parent.getCanonicalFile();
        if(!anchor.isDirectory())throw new IOException("Private directory unavailable");
        return anchor;
    }
    private static File leaf(File parent,String name) throws IOException {
        if(parent==null || name==null || name.isEmpty() || name.contains("/") || name.contains("\\") || name.equals(".") || name.equals(".."))throw new IOException("Invalid private directory");
        if(!parent.getAbsoluteFile().equals(parent.getCanonicalFile()))throw new IOException("Private parent changed");
        return new File(parent,name);
    }
    static void removeChild(File parent,String name) throws IOException { remove(leaf(directory(parent),name),null); }
    private static void remove(File value,LinkInspector links) throws IOException {
        // lstat inspects even a dangling link. Unlink this owned leaf only;
        // never recurse into its destination, whether inside or outside the app.
        if(links!=null && links.isSymbolicLink(value)) {
            if(!value.delete())throw new IOException("Private link could not be removed");
            return;
        }
        if(!value.getAbsoluteFile().equals(value.getCanonicalFile()))throw new IOException("Linked private file");
        if(!value.exists())return;
        if(value.isDirectory()) {
            File[] rows=value.listFiles();if(rows==null)throw new IOException("Private directory unavailable");
            for(File row:rows)remove(leaf(value,row.getName()),links);
        }
        if(!value.delete())throw new IOException("Private file could not be removed");
    }
    static void clearMemberFiles(File files,File cache,File data) throws IOException {
        clearMemberFiles(files,cache,data,null);
    }
    static void clearMemberFiles(File files,File cache,File data,LinkInspector links) throws IOException {
        File checkedData=directory(data),checkedFiles=directory(files),checkedCache=directory(cache);
        if(!checkedData.equals(checkedFiles.getParentFile()) || !checkedData.equals(checkedCache.getParentFile()))throw new IOException("Private roots do not match");
        remove(leaf(checkedFiles,"member"),links);
        // update may already be held by Android's installer. Keep that handoff.
        File[] rows=checkedCache.listFiles();if(rows==null)throw new IOException("Cache unavailable");
        for(File row:rows)if(!row.getName().equals("update"))remove(leaf(checkedCache,row.getName()),links);
        remove(leaf(checkedData,"app_webview"),links);
    }
    static void markComplete(File files) throws IOException {
        File target=child(files,"client-account-epoch"),temporary=child(files,"client-account-epoch.tmp");
        try(FileOutputStream output=new FileOutputStream(temporary)) {
            output.write(EPOCH.getBytes(StandardCharsets.US_ASCII));output.getFD().sync();
        }
        if(!temporary.renameTo(target))throw new IOException("Reset marker unavailable");
    }
}

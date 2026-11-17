package cn.healthlife.local;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Checks only the files Chaquopy 17.0 prepares before Py_InitializeEx. */
final class PythonBootstrapIntegrity {
    interface AssetSource { InputStream open(String path) throws IOException; }
    interface LinkInspector { boolean isSymbolicLink(File path) throws IOException; }
    interface HashStore { boolean invalidate(Set<String> keys) throws IOException; }
    interface Extractor { String extract() throws IOException; }

    static final class Selection {
        final String abi;
        final Map<String,String> assets;
        Selection(String abi,Map<String,String> assets) {
            this.abi=abi;this.assets=Collections.unmodifiableMap(assets);
        }
    }
    static final class Result {
        final String path;
        final boolean repaired;
        Result(String path,boolean repaired) {this.path=path;this.repaired=repaired;}
    }
    private static final class Fingerprint {
        final long bytes;
        final String sha1;
        Fingerprint(long bytes,String sha1) {this.bytes=bytes;this.sha1=sha1;}
        boolean same(Fingerprint other) {return bytes==other.bytes && sha1.equals(other.sha1);}
    }
    // These are exactly AndroidPlatform 17.0's deleteObsolete targets.
    private static final String[] OBSOLETE_FILES={"app.zip","requirements.zip","chaquopy.mp3",
        "stdlib.mp3","chaquopy.zip","lib-dynload","stdlib.zip","bootstrap.zip","stdlib-common.zip","ticket.txt"};
    private PythonBootstrapIntegrity() { }

    static Selection select(Map<String,String> manifest,String[] supportedAbis) throws IOException {
        for(Map.Entry<String,String> entry:manifest.entrySet()) {
            relativePath(entry.getKey());
            if(entry.getValue()==null || !entry.getValue().matches("[0-9a-f]{40}"))throw unavailable();
        }
        String abi=null;
        for(String value:supportedAbis) {
            if(value==null || !value.matches("[A-Za-z0-9_-]+"))throw unavailable();
            if(manifest.containsKey("stdlib-"+value+".imy")) {abi=value;break;}
        }
        if(abi==null)throw unavailable();
        Map<String,String> selected=new LinkedHashMap<>();
        for(String path:new String[]{"stdlib-common.imy","bootstrap.imy","cacert.pem"}) {
            if(!manifest.containsKey(path))throw unavailable();
            selected.put(path,manifest.get(path));
        }
        List<String> names=new ArrayList<>(manifest.keySet());Collections.sort(names);
        String prefix="bootstrap-native/"+abi+"/";
        for(String path:names)if(path.startsWith(prefix))selected.put(path,manifest.get(path));
        if(selected.size()==3)throw unavailable();
        return new Selection(abi,selected);
    }

    static Result prepare(File files,File cache,Selection selection,AssetSource source,
            LinkInspector links,HashStore hashes,Extractor sdk) throws IOException {
        // Context-owned ancestors may have a system alias. Everything below
        // these canonical roots must remain a real file/directory, never a link.
        File filesRoot=trustedRoot(files),cacheRoot=trustedRoot(cache);
        guardSdkPaths(filesRoot,cacheRoot,selection,links);
        Map<String,Fingerprint> expected=new LinkedHashMap<>();
        Set<String> invalid=new LinkedHashSet<>();
        for(Map.Entry<String,String> entry:selection.assets.entrySet()) {
            Fingerprint packed;
            try(InputStream stream=source.open(entry.getKey())) {packed=fingerprint(stream);}
            if(packed.bytes==0 || !packed.sha1.equals(entry.getValue()))throw unavailable();
            expected.put(entry.getKey(),packed);
            File extracted=asset(filesRoot,entry.getKey(),links);
            if(extracted.exists() && !packed.same(fingerprint(extracted)))invalid.add("asset."+entry.getKey());
        }
        // Missing files already force SDK extraction. Only content-corrupt,
        // existing assets need their individual cached hash invalidated.
        if(!invalid.isEmpty() && !hashes.invalidate(Collections.unmodifiableSet(invalid)))throw unavailable();
        String path=sdk.extract();
        guardSdkPaths(filesRoot,cacheRoot,selection,links);
        for(Map.Entry<String,Fingerprint> entry:expected.entrySet()) {
            File extracted=asset(filesRoot,entry.getKey(),links);
            if(!extracted.isFile() || !entry.getValue().same(fingerprint(extracted)))throw unavailable();
        }
        if(path==null || path.isEmpty())throw unavailable();
        return new Result(path,!invalid.isEmpty());
    }

    private static void guardSdkPaths(File files,File cache,Selection selection,LinkInspector links) throws IOException {
        File runtime=child(files,"chaquopy",links);
        // cleanExtractedDir traverses every existing child of this one ABI.
        guardTree(child(runtime,"bootstrap-native/"+selection.abi,links),links);
        for(String name:OBSOLETE_FILES)guardTree(child(runtime,name,links),links);
        File cacheRuntime=child(cache,"chaquopy",links);
        guardTree(child(cacheRuntime,"AssetFinder",links),links);
        for(String path:selection.assets.keySet()) {
            File target=child(runtime,path,links);
            if(target.exists() && !target.isFile())throw unavailable();
            File temporary=child(runtime,path+".tmp",links);
            if(temporary.exists() && !temporary.isFile())throw unavailable();
        }
    }

    private static File trustedRoot(File root) throws IOException {
        File real=root.getCanonicalFile();
        if(!real.isDirectory())throw unavailable();
        return real;
    }
    private static File asset(File files,String path,LinkInspector links) throws IOException {
        File target=child(child(files,"chaquopy",links),path,links);
        if(target.exists() && !target.isFile())throw unavailable();
        return target;
    }
    private static File child(File root,String relative,LinkInspector links) throws IOException {
        relativePath(relative);
        checkNode(root,links);
        if(root.exists() && !root.isDirectory())throw unavailable();
        File path=root;
        String[] parts=relative.split("/");
        for(int index=0;index<parts.length;index++) {
            path=new File(path,parts[index]);checkNode(path,links);
            if(index<parts.length-1 && path.exists() && !path.isDirectory())throw unavailable();
        }
        return path;
    }
    private static void relativePath(String value) throws IOException {
        if(value==null || value.isEmpty() || value.indexOf('\\')>=0 || value.indexOf(':')>=0)throw unavailable();
        for(String part:value.split("/",-1)) {
            if(part.isEmpty() || part.equals(".") || part.equals("..") || !part.matches("[A-Za-z0-9_.-]+"))throw unavailable();
        }
    }
    private static void checkNode(File path,LinkInspector links) throws IOException {
        if(links.isSymbolicLink(path) || !path.getAbsoluteFile().equals(path.getCanonicalFile()))throw unavailable();
        if(path.exists() && !path.isFile() && !path.isDirectory())throw unavailable();
    }
    private static void guardTree(File root,LinkInspector links) throws IOException {
        ArrayDeque<File> pending=new ArrayDeque<>();pending.add(root);
        while(!pending.isEmpty()) {
            File path=pending.removeLast();checkNode(path,links);
            if(path.isDirectory()) {
                File[] children=path.listFiles();if(children==null)throw unavailable();
                Collections.addAll(pending,children);
            }
        }
    }
    private static Fingerprint fingerprint(File file) throws IOException {
        if(!file.isFile())throw unavailable();
        try(InputStream stream=new FileInputStream(file)) {return fingerprint(stream);}
    }
    private static Fingerprint fingerprint(InputStream stream) throws IOException {
        MessageDigest digest;
        try {digest=MessageDigest.getInstance("SHA-1");}
        catch(NoSuchAlgorithmException impossible) {throw unavailable();}
        byte[] buffer=new byte[32768];long size=0;int count;
        while((count=stream.read(buffer))!=-1) {digest.update(buffer,0,count);size+=count;}
        StringBuilder value=new StringBuilder(40);
        for(byte part:digest.digest())value.append(String.format(java.util.Locale.ROOT,"%02x",part&255));
        return new Fingerprint(size,value.toString());
    }
    private static IOException unavailable() {return new IOException("Python bootstrap unavailable");}
}

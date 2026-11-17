package cn.healthlife.local;

import java.net.URI;
import java.net.InetAddress;
import java.util.Locale;

/** An article link opens in the user's browser, without app credentials. */
final class ReadingLinkPolicy {
    private ReadingLinkPolicy() {}

    static String externalUrl(String value) {
        if (value == null || value.isEmpty() || value.length() > 4096
                || !value.equals(value.trim()) || value.matches(".*[\\x00-\\x20\\x7f\\\\].*")) {
            throw new IllegalArgumentException("Invalid reading link");
        }
        final URI uri;
        try { uri = new URI(value); }
        catch (Exception ignored) { throw new IllegalArgumentException("Invalid reading link"); }
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        String host = uri.getHost() == null ? "" : uri.getHost().toLowerCase(Locale.ROOT);
        if (host.endsWith(".")) host = host.substring(0, host.length() - 1);
        if (!(scheme.equals("https") || scheme.equals("http")) || host.isEmpty()
                || uri.getRawUserInfo() != null || uri.getPort() < -1 || uri.getPort() == 0 || uri.getPort() > 65535
                || host.equals("localhost") || host.endsWith(".localhost") || host.endsWith(".local")
                || host.equals("appassets.androidplatform.net") || host.equals("appassets.androidplatform.net.")) {
            throw new IllegalArgumentException("Invalid reading link");
        }
        host = host.replace("[", "").replace("]", "");
        if (host.contains(":")) {
            final InetAddress address;
            try { address = InetAddress.getByName(host); }
            catch (Exception ignored) { throw new IllegalArgumentException("Invalid reading link"); }
            if (address.isAnyLocalAddress() || address.isLoopbackAddress() || address.isLinkLocalAddress()
                    || address.isSiteLocalAddress() || address.isMulticastAddress() || host.startsWith("fc") || host.startsWith("fd")) {
                throw new IllegalArgumentException("Local reading link");
            }
        } else if (host.matches("[0-9.]+")) {
            String[] parts = host.split("\\.", -1);
            if (parts.length != 4) throw new IllegalArgumentException("Invalid reading link");
            int[] octets = new int[4];
            for (int index = 0; index < 4; index++) {
                if (parts[index].isEmpty() || parts[index].length() > 3
                        || parts[index].length() > 1 && parts[index].startsWith("0")) {
                    throw new IllegalArgumentException("Invalid reading link");
                }
                octets[index] = Integer.parseInt(parts[index]);
                if (octets[index] > 255) throw new IllegalArgumentException("Invalid reading link");
            }
            if (octets[0] == 0 || octets[0] == 10 || octets[0] == 127 || octets[0] >= 224
                    || octets[0] == 169 && octets[1] == 254
                    || octets[0] == 172 && octets[1] >= 16 && octets[1] <= 31
                    || octets[0] == 192 && octets[1] == 168) {
                throw new IllegalArgumentException("Local reading link");
            }
        } else if (host.matches("0x[0-9a-f]+")) {
            throw new IllegalArgumentException("Invalid reading link");
        }
        return uri.toASCIIString();
    }
}

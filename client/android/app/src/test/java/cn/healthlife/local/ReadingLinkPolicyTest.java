package cn.healthlife.local;

import org.junit.Test;
import static org.junit.Assert.*;

public final class ReadingLinkPolicyTest {
    @Test public void ordinarySourceLinksKeepQueryAndAnchor() {
        String url = "https://mp.weixin.qq.com/s?__biz=abc&mid=42#section";
        assertEquals(url, ReadingLinkPolicy.externalUrl(url));
        assertEquals("http://example.org/original", ReadingLinkPolicy.externalUrl("http://example.org/original"));
        assertEquals("https://example.org/%E5%8E%9F%E6%96%87", ReadingLinkPolicy.externalUrl("https://example.org/原文"));
    }

    @Test public void appLocalExecutableAndCredentialLinksAreRejected() {
        String[] bad = {"", " https://example.org", "https://example.org/a\nb", "javascript:alert(1)",
            "file:///data/user/0/private", "content://private/data", "intent://example.org", "data:text/html,test",
            "https://user:pass@example.org/", "https://appassets.androidplatform.net/assets/index.html",
            "https://localhost/", "https://localhost./", "https://a.localhost/", "http://127.0.0.1/", "http://10.1.2.3/",
            "http://192.168.0.1/", "http://172.20.1.1/", "http://169.254.1.1/", "http://2130706433/",
            "http://0177.0.0.1/", "http://0x7f000001/", "http://[::1]/", "http://[0:0:0:0:0:0:0:1]/", "http://[::ffff:127.0.0.1]/", "http://[fd00::1]/",
            "https://example.org:0/", "https://example.org:65536/", "https://example.org\\@localhost/"};
        for (String value : bad) {
            try { ReadingLinkPolicy.externalUrl(value); fail("Accepted invalid reading link"); }
            catch (IllegalArgumentException expected) { /* Only ordinary public web links. */ }
        }
    }
}

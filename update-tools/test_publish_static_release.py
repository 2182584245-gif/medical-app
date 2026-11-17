"""Offline publisher policy and real filesystem publication tests; no sockets."""
from contextlib import redirect_stdout
import hashlib
from io import BytesIO, StringIO
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
BASE = 'https://updates.example.invalid/mobile-updates/'
spec = importlib.util.spec_from_file_location('publisher', HERE/'publish_static_release.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='publisher-', dir=HERE)
        self.root = Path(self.temp.name)
        self.public = self.root/'public'; self.private = self.root/'private'
        self.public.mkdir(); self.private.mkdir(mode=0o700)
        self.apk = self.root/'staged.apk'; self.apk.write_bytes(b'known synthetic APK bytes')
        self.audit = self.root/'audit.json'
        self.notes = self.root/'notes.json'; self.notes.write_text(json.dumps(['新增应用内更新', '保留原有资料'], ensure_ascii=False),encoding='utf-8')
        self.audit.write_text(json.dumps({'passed':True,'checks':{'independent_signature_and_bytes':True},'apk':{
            'identity':{'application_id':'com.fawnsee.mobile','version_code':10133,'version_name':'1.1.3'},
            'bytes':self.apk.stat().st_size,'sha256':publisher.sha(self.apk),'min_sdk':24}}),encoding='utf-8')
        self.no_network = patch.object(socket.socket,'connect',side_effect=AssertionError('Offline suite forbids network'))
        self.no_network.start(); self.addCleanup(self.no_network.stop)
        if os.name == 'nt':
            # POSIX rename can replace a read-only file in a writable directory;
            # Windows chmod instead sets a target-file read-only attribute.
            real_replace = os.replace
            def posix_replace(source, destination):
                target=Path(destination)
                self.assertTrue(target.is_relative_to(self.root))
                if target.exists(): target.chmod(0o600)
                return real_replace(source,destination)
            self.adapter = patch.object(publisher.os,'replace',side_effect=posix_replace)
            self.adapter.start(); self.addCleanup(self.adapter.stop)
            real_unlink=Path.unlink
            def posix_unlink(target,missing_ok=False):
                self.assertTrue(target.is_relative_to(self.root))
                if target.exists():target.chmod(0o600)
                return real_unlink(target,missing_ok=missing_ok)
            unlink_adapter=patch.object(Path,'unlink',new=posix_unlink)
            unlink_adapter.start();self.addCleanup(unlink_adapter.stop)
            directory_fsync=patch.object(publisher,'fsync_directory',side_effect=lambda p:self.assertTrue(p.is_relative_to(self.root)))
            directory_fsync.start();self.addCleanup(directory_fsync.stop)

    def tearDown(self):
        for path in self.root.rglob('*'):
            if path.is_file(): path.chmod(0o600)
        self.temp.cleanup()

    def run_publish(self, apply=True, download=None, code=10133):
        argv=['publish_static_release.py','--release-root',str(self.public),'--state-dir',str(self.private),
              '--staged-apk',str(self.apk),'--audit-json',str(self.audit),'--version-code',str(code),
              '--release-notes',str(self.notes),'--base-url',BASE,'--version-name','1.1.3']
        if apply: argv.append('--apply')
        with patch.object(sys,'argv',argv), patch.object(publisher,'download_verified',side_effect=download or self.accept_download) as mock, redirect_stdout(StringIO()) as output:
            publisher.main()
        return json.loads(output.getvalue()),mock

    def accept_download(self,url,size,digest,immutable=False):
        path=self.public/url.rsplit('/',1)[1]
        self.assertTrue(path.is_file())
        self.assertEqual(size,path.stat().st_size)
        self.assertEqual(digest,publisher.sha(path))
        return {'url':url,'bytes':size,'sha256':digest,'tls_verified':True,'redirects':False}

    def prior(self,code=10132):
        value={'schema_version':1,'application_id':'com.fawnsee.mobile','version_code':code,'marker':'original'}
        content=json.dumps(value).encode();(self.public/'latest.json').write_bytes(content)
        return content

    def test_dry_run_has_string_notes_and_no_files_or_requests(self):
        result,mock=self.run_publish(apply=False)
        self.assertEqual('新增应用内更新\n保留原有资料',result['manifest']['release_notes'])
        self.assertEqual(set(),set(self.public.iterdir()))
        mock.assert_not_called()
        self.assertEqual({'schema_version','application_id','version_code','version_name','min_sdk','size_bytes','sha256','apk_url','published_at','release_notes'},set(result['manifest']))

    def test_explicit_update_url_appears_in_the_manifest(self):
        result, _ = self.run_publish(apply=False)
        self.assertEqual(BASE + 'FAWNSEE-MOBILE-10133.apk', result['manifest']['apk_url'])

    def test_update_url_refuses_non_https_and_ambiguous_paths(self):
        for url in (
            'http://updates.example.invalid/mobile-updates/',
            'https://alice:secret@updates.example.invalid/mobile-updates/',
            'https://updates.example.invalid:443/mobile-updates/',
            'https://updates.example.invalid/mobile-updates/?token=example',
            'https://updates.example.invalid/mobile-updates/#part',
            'https://updates.example.invalid/mobile-updates',
            'https://updates.example.invalid/mobile-updates/../',
            'https://updates.example.invalid/mobile-updates/%2e%2e/',
            'https://updates.example.invalid//mobile-updates/',
        ):
            self.assertRaises(publisher.argparse.ArgumentTypeError, publisher.update_base, url)

    def test_apk_is_public_and_verified_before_atomic_latest(self):
        original=self.prior();calls=[]
        def readback(url,size,digest,immutable=False):
            calls.append(immutable)
            if immutable:self.assertEqual(original,(self.public/'latest.json').read_bytes())
            else:self.assertEqual(10133,json.loads((self.public/'latest.json').read_bytes())['version_code'])
            return self.accept_download(url,size,digest,immutable)
        result,_=self.run_publish(download=readback)
        self.assertTrue(result['passed']);self.assertEqual([True,False],calls)
        self.assertTrue((self.private/'publication-10133.json').is_file())
        self.assertFalse(any(p.name.endswith('.lock') for p in self.private.iterdir()))

    def test_apk_hash_readback_failure_leaves_original_latest(self):
        original=self.prior()
        with self.assertRaisesRegex(ValueError,'HTTPS failed'):
            self.run_publish(download=lambda *a,**k:(_ for _ in ()).throw(ValueError('HTTPS failed')))
        self.assertEqual(original,(self.public/'latest.json').read_bytes())
        self.assertTrue((self.public/'FAWNSEE-MOBILE-10133.apk').is_file())

    def test_latest_readback_failure_restores_exact_original(self):
        original=self.prior()
        def readback(url,size,digest,immutable=False):
            if not immutable:raise ValueError('latest failed')
            return self.accept_download(url,size,digest,immutable)
        with self.assertRaisesRegex(ValueError,'latest failed'):self.run_publish(download=readback)
        self.assertEqual(original,(self.public/'latest.json').read_bytes())
        self.assertTrue((self.public/'FAWNSEE-MOBILE-10133.apk').is_file())

    def test_first_publication_failure_removes_only_own_latest(self):
        def readback(url,size,digest,immutable=False):
            if not immutable:raise ValueError('latest failed')
            return self.accept_download(url,size,digest,immutable)
        with self.assertRaisesRegex(ValueError,'latest failed'):self.run_publish(download=readback)
        self.assertFalse((self.public/'latest.json').exists())
        self.assertEqual(self.apk.read_bytes(),(self.public/'FAWNSEE-MOBILE-10133.apk').read_bytes())

    def test_different_existing_version_bytes_are_never_overwritten(self):
        target=self.public/'FAWNSEE-MOBILE-10133.apk';target.write_bytes(b'other artifact')
        with self.assertRaisesRegex(ValueError,'never overwrite'):self.run_publish()
        self.assertEqual(b'other artifact',target.read_bytes())

    def test_staged_apk_must_match_independent_audit(self):
        self.apk.write_bytes(b'mutated bytes')
        with self.assertRaisesRegex(ValueError,'independently audited'):self.run_publish()
        self.assertFalse(list(self.public.iterdir()))

    def test_manifest_minimum_sdk_is_taken_from_independent_apk_audit(self):
        value=json.loads(self.audit.read_text());value['apk']['min_sdk']=26
        self.audit.write_text(json.dumps(value))
        result,_=self.run_publish(apply=False)
        self.assertEqual(26,result['manifest']['min_sdk'])

    def test_missing_actual_minimum_sdk_cannot_publish(self):
        value=json.loads(self.audit.read_text());del value['apk']['min_sdk']
        self.audit.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'minimum SDK'):self.run_publish()

    def test_downgrade_is_refused_without_network(self):
        original=self.prior(10134)
        with self.assertRaisesRegex(ValueError,'downgrade'):self.run_publish()
        self.assertEqual(original,(self.public/'latest.json').read_bytes())

    def test_public_directory_never_exposes_non_release_files(self):
        (self.public/'preferences.py').write_text('private source')
        with self.assertRaisesRegex(ValueError,'non-release'):self.run_publish()

    def test_bad_native_note_string_contract_is_refused(self):
        self.notes.write_text(json.dumps([' unwanted padding ']),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'trimmed'):self.run_publish()

    def test_utf16_note_bound_matches_native_java(self):
        self.notes.write_text(json.dumps(['😀'*500 for _ in range(9)]),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'bounded plain-text'):self.run_publish()

    def test_does_not_clobber_newer_external_latest_during_rollback(self):
        self.prior();external=b'{"external":"new publisher"}'
        def readback(url,size,digest,immutable=False):
            if immutable:return self.accept_download(url,size,digest,immutable)
            latest=self.public/'latest.json';latest.chmod(0o600);latest.write_bytes(external)
            raise ValueError('readback failed')
        with self.assertRaisesRegex(ValueError,'Another publisher'):self.run_publish(download=readback)
        self.assertEqual(external,(self.public/'latest.json').read_bytes())


class TransportTests(unittest.TestCase):
    class Response(BytesIO):
        status=200
        def __init__(self,body,url,cache):super().__init__(body);self.url=url;self.headers={'Cache-Control':cache}
        def geturl(self):return self.url
    class Opener:
        def __init__(self,response):self.response=response;self.request=None
        def open(self,request,timeout):self.request=request;return self.response

    def verify(self,body,cache='public,max-age=31536000,immutable',expected=None,immutable=True):
        url=BASE+'FAWNSEE-MOBILE-10133.apk'
        opener=self.Opener(self.Response(body,url,cache))
        with patch.object(publisher.urllib.request,'build_opener',return_value=opener) as build:
            result=publisher.download_verified(url,len(body),expected or hashlib.sha256(body).hexdigest(),immutable)
        handlers=build.call_args.args
        self.assertTrue(any(isinstance(h,publisher.urllib.request.ProxyHandler) and h.proxies=={} for h in handlers))
        self.assertTrue(any(isinstance(h,publisher.NoRedirect) for h in handlers))
        self.assertEqual('identity',opener.request.get_header('Accept-encoding'))
        return result

    def test_strict_https_handler_and_no_proxy_no_redirect_contract(self):
        self.assertTrue(self.verify(b'known bytes')['tls_verified'])
    def test_body_hash_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'hash'):self.verify(b'known bytes',expected='0'*64)
    def test_immutable_cache_required(self):
        with self.assertRaisesRegex(ValueError,'immutable cache'):self.verify(b'known bytes',cache='no-store')
    def test_latest_cache_must_be_no_store(self):
        with self.assertRaisesRegex(ValueError,'no-store'):self.verify(b'latest',immutable=False,cache='public,max-age=30')
    def test_redirect_handler_never_follows_location(self):
        with self.assertRaisesRegex(ValueError,'refuses redirects'):
            publisher.NoRedirect().redirect_request(None,None,302,'redirect',{},'https://example.invalid/')
    def test_compressed_header_is_rejected_even_when_body_hash_matches(self):
        body=b'known bytes';url=BASE+'FAWNSEE-MOBILE-10133.apk'
        response=self.Response(body,url,'public,max-age=31536000,immutable')
        response.headers['Content-Encoding']='gzip'
        with patch.object(publisher.urllib.request,'build_opener',return_value=self.Opener(response)):
            with self.assertRaisesRegex(ValueError,'identity-encoded'):
                publisher.download_verified(url,len(body),hashlib.sha256(body).hexdigest(),True)


if __name__=='__main__':
    unittest.main(verbosity=2)

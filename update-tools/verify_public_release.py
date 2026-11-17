"""Opt-in independent client-side verification of the public update channel.

Default only prints the plan. --run makes two unauthenticated strict-HTTPS GETs
and streams the APK through SHA256 without saving or installing it.
"""
from pathlib import Path
import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
import ssl
import time
import urllib.request
from publish_static_release import APPLICATION_ID, NoRedirect, require, update_base


def verify_apk(opener, url, size, expected_sha, on_progress=lambda count:None):
    """Bounded ordinary HTTPS readback; no APK file is created."""
    request=urllib.request.Request(url,headers={'Cache-Control':'no-cache','Accept-Encoding':'identity','User-Agent':'FAWNSEE-public-channel-verifier/1'})
    with opener.open(request,timeout=60) as response:
        require(response.status==200 and response.geturl()==url,'Unexpected public APK response')
        require(response.headers.get('Content-Encoding','identity').lower()=='identity','Public APK encoding differs from native policy')
        cache=response.headers.get('Cache-Control','').lower()
        directives=[term.strip() for term in cache.split(',')]
        require('immutable' in directives and any(re.fullmatch(r'max-age\s*=\s*31536000',term) for term in directives),'Public APK immutable cache policy differs')
        digest=hashlib.sha256();count=0
        for chunk in iter(lambda:response.read(256*1024),b''):
            count+=len(chunk)
            require(count<=size,'Public APK exceeds independently audited size')
            digest.update(chunk);on_progress(count)
    require(count==size and digest.hexdigest()==expected_sha,'Public APK size/hash differs from independent audit')
    return {'url':url,'bytes':count,'sha256':digest.hexdigest(),'cache_control':cache,'tls_verified':True,'redirects':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True, type=update_base)
    parser.add_argument('--audit-json',required=True,type=Path)
    parser.add_argument('--report',required=True,type=Path)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--progress',action='store_true',help='Emit real bytes-read progress and save in-progress metadata during a slow full APK readback')
    args=parser.parse_args()
    audit=json.loads(args.audit_json.read_text(encoding='utf-8'))
    require(audit.get('passed') is True and audit.get('checks') and all(x is True for x in audit['checks'].values()),'Independent audit is not fully green')
    apk=audit['apk'];identity=apk['identity']
    require(identity['application_id']==APPLICATION_ID and type(identity['version_code']) is int,'Unexpected audited app identity')
    expected_url=args.base_url+'FAWNSEE-MOBILE-'+str(identity['version_code'])+'.apk'
    report={'checked_at_utc':datetime.now(timezone.utc).isoformat(),'mode':'live' if args.run else 'dry-run','requests_planned':[args.base_url+'latest.json',expected_url],
            'authenticated_requests':False,'apk_installed':False,'apk_saved':False,'requests_sent':0}
    if args.run:
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            request=urllib.request.Request(args.base_url+'latest.json',headers={'Cache-Control':'no-cache','Accept-Encoding':'identity','User-Agent':'FAWNSEE-public-channel-verifier/1'})
            report['requests_sent']+=1
            with opener.open(request,timeout=30) as response:
                require(response.status==200 and response.geturl()==args.base_url+'latest.json','Unexpected public manifest response')
                cache=response.headers.get('Cache-Control','')
                require('no-store' in cache.lower(),'Public manifest cache is not no-store')
                require(response.headers.get('Content-Encoding','identity').lower()=='identity','Public manifest encoding differs from native policy')
                raw=response.read(32*1024+1)
                require(0<len(raw)<=32*1024,'Public manifest body exceeds native limit')
            manifest=json.loads(raw.decode('utf-8'))
            require(set(manifest)=={'schema_version','application_id','version_name','version_code','min_sdk','size_bytes','sha256','apk_url','published_at','release_notes'},'Manifest schema differs from native contract')
            require(type(manifest['schema_version']) is int and manifest['schema_version']==1,'Manifest schema version differs')
            for key,value in identity.items():require(manifest[key]==value,'Manifest app identity differs from audited APK')
            require(type(manifest['size_bytes']) is int and manifest['size_bytes']==apk['bytes'] and manifest['sha256']==apk['sha256'],'Manifest APK size/hash differs from independent audit')
            require(manifest['min_sdk']==apk['min_sdk'],'Manifest minimum SDK differs from actual APK')
            require(manifest['apk_url']==expected_url,'Manifest download URL differs from fixed origin/version path')
            require(isinstance(manifest['release_notes'],str) and 0<len(manifest['release_notes'].encode('utf-16-le'))//2<=8000,'Release notes differ from native string contract')
            report['manifest']={'url':args.base_url+'latest.json','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'cache_control':cache,'identity_and_artifact_match':True}
            report['requests_sent']+=1
            last_progress=[0.0]
            def progress(count):
                now=time.monotonic()
                if not args.progress or now-last_progress[0]<5:return
                last_progress[0]=now
                report['progress']={'phase':'apk_readback','bytes_read':count,'total_bytes':apk['bytes'],'percent':round(count*100/apk['bytes'],2)}
                args.report.parent.mkdir(parents=True,exist_ok=True)
                args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({'event':'APK_READBACK_PROGRESS',**report['progress']}),flush=True)
            report['apk_https']=verify_apk(opener,expected_url,apk['bytes'],apk['sha256'],progress)
            report['progress']={'phase':'complete','bytes_read':apk['bytes'],'total_bytes':apk['bytes'],'percent':100}
            report['passed']=True
            report['finished_utc']=datetime.now(timezone.utc).isoformat()
        except Exception as error:
            report['passed']=False
            report['error_type']=type(error).__name__
            # Do not retain arbitrary server bodies or authentication data.
            args.report.parent.mkdir(parents=True,exist_ok=True)
            args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            raise
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    main()

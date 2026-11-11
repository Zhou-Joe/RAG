"""Standalone FOS Knowledge Feed v1 push client (Python standard library only)."""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urljoin,urlsplit
from urllib.request import Request,urlopen,HTTPRedirectHandler,build_opener
from urllib.error import HTTPError

SCHEMA='fos-rag-feed/v1'
MAX_BYTES=500_000_000

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


def push(url: str,token: str,batch: dict,*,wait: bool=True,timeout: int=660) -> dict:
    """Submit once, then poll that run; never resend with a different batch ID silently."""
    parts=urlsplit(url)
    if parts.scheme not in ('http','https') or not parts.netloc or parts.username or parts.password:raise ValueError('Use an HTTP(S) ingest URL without embedded credentials.')
    if not token or '\n' in token or '\r' in token:raise ValueError('Missing or invalid feed token.')
    raw=json.dumps(batch,ensure_ascii=False,allow_nan=False).encode('utf-8')
    if len(raw)>MAX_BYTES:raise ValueError('Batch exceeds 500MB.')
    opener=build_opener(NoRedirect())
    def request(target,data=None):
        try:
            with opener.open(Request(target,data=data,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'}),timeout=30) as response:
                body=response.read(MAX_BYTES+1)
                if len(body)>MAX_BYTES:raise ValueError('Response too large.')
                result=json.loads(body)
                if not result.get('ok'):raise ValueError(result.get('error','Feed request failed.'))
                return result
        except HTTPError as exc:
            # Avoid printing URLs, headers or token-bearing response bodies.
            raise RuntimeError('Feed request returned HTTP '+str(exc.code)+'.') from None
    result=request(url,raw)
    if wait and result['run']['status']=='running':
        status=urljoin(url,result['status_url'])
        if urlsplit(status)[:2]!=urlsplit(url)[:2]:raise ValueError('Refused a status URL on another origin.')
        started=time.monotonic()
        while result['run']['status']=='running':
            if time.monotonic()-started>timeout:raise TimeoutError('Run is still pending. Check its run ID before resubmitting.')
            time.sleep(2);result=request(status)
    if result['run']['status']=='failed':raise RuntimeError('Feed run failed: '+result['run'].get('error',''))
    return result


def main():
    parser=argparse.ArgumentParser(description='Push a fos-rag-feed/v1 batch. Keep credentials in environment variables.')
    parser.add_argument('path',help='JSON file, or - for stdin')
    parser.add_argument('--url',default=os.environ.get('FOS_FEED_INGEST_URL',''))
    parser.add_argument('--token-env',default='FOS_FEED_PUSH_TOKEN')
    parser.add_argument('--no-wait',action='store_true')
    args=parser.parse_args()
    if args.path=='-':raw=sys.stdin.buffer.read(MAX_BYTES+1)
    else:
        with Path(args.path).open('rb') as stream:raw=stream.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:raise ValueError('Batch exceeds 500MB.')
    result=push(args.url,os.environ.get(args.token_env,''),json.loads(raw),wait=not args.no_wait)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as exc:print(type(exc).__name__+': '+str(exc),file=sys.stderr);sys.exit(1)

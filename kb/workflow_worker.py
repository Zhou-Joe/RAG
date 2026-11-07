"""Isolated Microsoft workflow delivery. Never print URLs, secrets, payloads or response bodies."""
import json
import sys
from urllib.parse import urlsplit

def main():
    import httpx
    try:
        request=json.loads(sys.stdin.buffer.read(2_000_001))
        url=request['url'];parsed=urlsplit(url);host=(parsed.hostname or '').lower()
        if parsed.scheme!='https' or parsed.port not in (None,443) or parsed.username or parsed.password or not any(host.endswith(s) for s in ('.environment.api.powerplatform.com','.logic.azure.com','.logic.azure.cn')) or '/triggers/' not in parsed.path or not parsed.path.endswith('/invoke'):raise ValueError()
        with httpx.Client(timeout=30,follow_redirects=False,trust_env=False) as client:
            # Stream headers only: workflow responses need not disclose their content.
            with client.stream('POST',url,json=request['body']) as response:
                result={'ok':200<=response.status_code<300,'status':response.status_code}
    except Exception:result={'ok':False,'status':'connection_failed'}
    sys.stdout.write(json.dumps(result))

if __name__=='__main__':main()

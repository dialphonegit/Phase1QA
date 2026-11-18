#!/usr/bin/env python3
"""
FusionPBX → DialPhone Queue Caller
Configures a SIP Gateway on FusionPBX using a DialPhone member credential
(SIP over WebSocket transport), then places N calls to queue ext 0200.

FusionPBX host:  https://65.20.93.58
FusionPBX login: devdialphone / Devdialphone@123!

DialPhone SIP:   wss://app.qa.unifiedrtc.com/ws/
Queue:           0200@1792517466513.unifiedrtc.com

Usage:
  python3 fusionpbx_queue_caller.py --calls 10 --queue-ext 0200
  python3 fusionpbx_queue_caller.py --list-gateways
"""
import argparse, json, re, sys, time, urllib.request, urllib.parse

FPBX_BASE  = 'https://65.20.93.58'
FPBX_USER  = 'devdialphone'
FPBX_PASS  = 'Devdialphone@123!'

SIP_WS_HOST = 'app.qa.unifiedrtc.com'
SIP_DOMAIN  = '1792517466513.unifiedrtc.com'
CREDS_FILE  = '/tmp/queue100_creds.json'


def load_cred(index=10):
    """Load a DialPhone SIP credential to use as the FusionPBX gateway trunk."""
    raw = json.load(open(CREDS_FILE))
    creds = raw if isinstance(raw, list) else raw.get('creds', [])
    c = creds[index]
    if 'pw' in c:
        c['password'] = c.pop('pw')
    if 'ext' in c:
        c['extension'] = c.pop('ext')
    if 'name' in c:
        c['sip_user'] = c.pop('name')
    return c


def fpbx_session():
    """Return a urllib opener with FusionPBX session cookie."""
    import urllib.request, ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor()
    )
    # Login
    data = urllib.parse.urlencode({
        'username': FPBX_USER, 'password': FPBX_PASS
    }).encode()
    req = urllib.request.Request(
        f'{FPBX_BASE}/core/user_auth/index.php',
        data=data,
        headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST'
    )
    opener.open(req, timeout=15)
    return opener


def get_csrf(opener, url):
    resp = opener.open(url, timeout=15)
    html = resp.read().decode('utf-8', errors='ignore')
    m = re.search(r'name=["\']token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m:
        m = re.search(r'token["\s:=]+["\']([a-f0-9]{32,})["\']', html)
    return m.group(1) if m else ''


def create_gateway(opener, cred, gateway_name='dialphone-queue'):
    """
    Create a SIP gateway in FusionPBX using a DialPhone WebSocket credential.
    FusionPBX (FreeSWITCH) supports WSS transport via transport=wss in the gateway.
    """
    gw_url = f'{FPBX_BASE}/app/gateways/gateway_edit.php'
    csrf   = get_csrf(opener, gw_url)

    params = {
        'token':           csrf,
        'action':          'add',
        'gateway_name':    gateway_name,
        'username':        cred['sip_user'],
        'password':        cred['password'],
        'proxy':           SIP_WS_HOST,
        'register':        'true',
        'register_transport': 'wss',
        'from_user':       cred['extension'],
        'from_domain':     SIP_DOMAIN,
        'expire_seconds':  '300',
        'enabled':         'true',
        'description':     'DialPhone queue test trunk',
    }
    data = urllib.parse.urlencode(params).encode()
    req  = urllib.request.Request(gw_url, data=data,
           headers={'User-Agent':'Mozilla/5.0','Content-Type':'application/x-www-form-urlencoded'},
           method='POST')
    try:
        resp = opener.open(req, timeout=15)
        return resp.status == 200 or resp.status == 302
    except Exception as e:
        print(f'[!] Gateway create error: {e}')
        return False


def originate_call(opener, destination, gateway_name='dialphone-queue', caller_id='1000'):
    """
    Use FusionPBX Quick Broadcast to originate a call through the gateway.
    The destination is 0200@SIP_DOMAIN via the dialphone gateway.
    """
    # First try ESL (Event Socket) originate via web API
    # FusionPBX exposes /app/switch/index.php for ESL commands
    url = f'{FPBX_BASE}/app/switch/index.php'
    csrf = get_csrf(opener, url)

    # sofia originate via gateway
    originate_str = (
        f'sofia/gateway/{gateway_name}/{destination} '
        f'&bridge(sofia/gateway/{gateway_name}/{destination})'
    )
    params = {
        'token':   csrf,
        'action':  'originate',
        'channel': originate_str,
    }
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data,
          headers={'User-Agent':'Mozilla/5.0','Content-Type':'application/x-www-form-urlencoded'},
          method='POST')
    try:
        resp = opener.open(req, timeout=15)
        body = resp.read().decode()
        return 'success' in body.lower() or resp.status < 400
    except Exception as e:
        print(f'[!] Originate error: {e}')
        return False


def quick_broadcast(opener, numbers, queue_ext='0200'):
    """Use FusionPBX Quick Broadcast to call numbers and bridge to queue."""
    url   = f'{FPBX_BASE}/app/quick_broadcast/quick_broadcast.php'
    csrf  = get_csrf(opener, url)

    # Number list — each number will be called, answered, and bridged to queue
    num_list = '\n'.join(str(n) for n in numbers)

    params = {
        'token':          csrf,
        'action':         'broadcast',
        'destination':    queue_ext,
        'number_list':    num_list,
        'caller_id_name': 'LoadTest',
        'caller_id_number': '1000',
        'timeout':        '30',
    }
    data = urllib.parse.urlencode(params).encode()
    req  = urllib.request.Request(url, data=data,
           headers={'User-Agent':'Mozilla/5.0','Content-Type':'application/x-www-form-urlencoded'},
           method='POST')
    resp = opener.open(req, timeout=20)
    body = resp.read().decode()
    m = re.search(r'(broadcast[^<.]*)', body, re.I)
    return m.group(1) if m else body[:200]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--calls',      type=int, default=10)
    p.add_argument('--queue-ext',  default='0200')
    p.add_argument('--setup-gateway', action='store_true', help='Create SIP gateway in FusionPBX')
    p.add_argument('--list-gateways', action='store_true')
    p.add_argument('--broadcast',  action='store_true', help='Use Quick Broadcast to place calls')
    args = p.parse_args()

    print('Logging into FusionPBX...')
    opener = fpbx_session()
    print('OK')

    if args.list_gateways:
        resp = opener.open(f'{FPBX_BASE}/app/gateways/gateways.php', timeout=15)
        html = resp.read().decode()
        gws  = re.findall(r'dialphone[^<"\']*', html, re.I)
        print('Gateways found:', gws or '(none)')
        return

    if args.setup_gateway:
        cred = load_cred()
        print(f'Creating gateway with cred ext={cred["extension"]} user={cred["sip_user"]}')
        ok = create_gateway(opener, cred)
        print('Gateway created OK' if ok else 'Gateway create FAILED')

    if args.broadcast:
        # Use Quick Broadcast — calls a list of numbers
        # For queue testing, call back numbers that agents have registered
        numbers = [f'1{i:03d}' for i in range(1, args.calls + 1)]
        print(f'Quick Broadcast: {len(numbers)} calls → queue {args.queue_ext}')
        result = quick_broadcast(opener, numbers, args.queue_ext)
        print(f'Result: {result}')
    else:
        print(f'Placing {args.calls} calls to queue {args.queue_ext} via ESL originate...')
        ok = 0
        for i in range(args.calls):
            if originate_call(opener, f'{args.queue_ext}@{SIP_DOMAIN}'):
                ok += 1
                print(f'  [{i+1}] ✅')
            else:
                print(f'  [{i+1}] ❌')
            time.sleep(0.5)
        print(f'\nResult: {ok}/{args.calls} calls placed')


if __name__ == '__main__':
    main()

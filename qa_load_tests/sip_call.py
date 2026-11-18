"""
SIP over WebSocket - Register two users and make an internal call between them.
Admin (ext 1001) calls Rashmi Tiwari (ext 9998).
"""
import asyncio
import websockets
import hashlib
import uuid
import time
import re

WSS_URL = "wss://app.qa.unifiedrtc.com/ws/"
DOMAIN = "1792517466513.unifiedrtc.com"

CALLER = {"ext": "1001", "name": "Aman Mehtar",   "pw": "$2b$12$H39.ZnHPmEXYWecOsx3HPOYh6BMn2oPX.uUtsl/JIaeH8TyABvjyu"}
CALLEE = {"ext": "9998", "name": "Rashmi Tiwari", "pw": "$2b$12$rrgFvPYwzoH/ib4vLgF5huM5jIvC7GLty3ESrXvQSQeRsBzMWDOFO"}

def make_via(branch):
    return f"SIP/2.0/WSS {DOMAIN};rport;branch={branch};alias"

def make_register(ext, name, cseq, call_id, branch, tag):
    contact = f"sip:{ext}@{DOMAIN};transport=ws"
    return (
        f"REGISTER sip:{DOMAIN} SIP/2.0\r\n"
        f"Via: {make_via(branch)}\r\n"
        f"From: \"{name}\" <sip:{ext}@{DOMAIN}>;tag={tag}\r\n"
        f"To: \"{name}\" <sip:{ext}@{DOMAIN}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} REGISTER\r\n"
        f"Contact: <{contact}>;+sip.ice;reg-id=1\r\n"
        f"Supported: path,gruu,outbound\r\n"
        f"Content-Length: 0\r\n\r\n"
    )

def make_register_auth(ext, name, password, cseq, call_id, branch, tag, www_auth):
    realm = re.search(r'realm="([^"]+)"', www_auth).group(1)
    nonce = re.search(r'nonce="([^"]+)"', www_auth).group(1)
    ha1 = hashlib.md5(f"{ext}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"REGISTER:sip:{DOMAIN}".encode()).hexdigest()
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    auth = (f'Digest username="{ext}", realm="{realm}", nonce="{nonce}", '
            f'uri="sip:{DOMAIN}", response="{response}", algorithm=MD5')
    contact = f"sip:{ext}@{DOMAIN};transport=ws"
    return (
        f"REGISTER sip:{DOMAIN} SIP/2.0\r\n"
        f"Via: {make_via(branch + 'a')}\r\n"
        f"From: \"{name}\" <sip:{ext}@{DOMAIN}>;tag={tag}\r\n"
        f"To: \"{name}\" <sip:{ext}@{DOMAIN}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} REGISTER\r\n"
        f"Contact: <{contact}>;+sip.ice;reg-id=1\r\n"
        f"Authorization: {auth}\r\n"
        f"Content-Length: 0\r\n\r\n"
    )

def make_invite(caller, callee, call_id, branch, tag):
    sdp = (
        "v=0\r\n"
        f"o={caller['ext']} {int(time.time())} {int(time.time())} IN IP4 0.0.0.0\r\n"
        "s=dialphone\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
    )
    return (
        f"INVITE sip:{callee['ext']}@{DOMAIN} SIP/2.0\r\n"
        f"Via: {make_via(branch)}\r\n"
        f"From: \"{caller['name']}\" <sip:{caller['ext']}@{DOMAIN}>;tag={tag}\r\n"
        f"To: \"{callee['name']}\" <sip:{callee['ext']}@{DOMAIN}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:{caller['ext']}@{DOMAIN};transport=ws>\r\n"
        f"Max-Forwards: 70\r\nContent-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp.encode())}\r\n\r\n{sdp}"
    )

def make_200ok(invite_msg, ext, name):
    call_id  = re.search(r'Call-ID: ([^\r\n]+)', invite_msg).group(1)
    from_h   = re.search(r'From: ([^\r\n]+)', invite_msg).group(1)
    to_h     = re.search(r'To: ([^\r\n]+)', invite_msg).group(1)
    cseq     = re.search(r'CSeq: ([^\r\n]+)', invite_msg).group(1)
    via      = re.search(r'Via: ([^\r\n]+)', invite_msg).group(1)
    sdp = (
        "v=0\r\n"
        f"o={ext} {int(time.time())} {int(time.time())} IN IP4 0.0.0.0\r\n"
        "s=dialphone\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
    )
    return (
        f"SIP/2.0 200 OK\r\nVia: {via}\r\nFrom: {from_h}\r\n"
        f"To: {to_h};tag={uuid.uuid4().hex[:8]}\r\n"
        f"Call-ID: {call_id}\r\nCSeq: {cseq}\r\n"
        f"Contact: <sip:{ext}@{DOMAIN};transport=ws>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp.encode())}\r\n\r\n{sdp}"
    )

async def register_ua(info):
    ext, name, pw = info['ext'], info['name'], info['password']
    ws = await websockets.connect(WSS_URL, additional_headers={'Sec-WebSocket-Protocol': 'sip'}, subprotocols=['sip'])
    print(f"[{ext}] WebSocket connected")

    call_id = f"{uuid.uuid4().hex}@{DOMAIN}"
    branch  = f"z9hG4bK{uuid.uuid4().hex[:16]}"
    tag     = uuid.uuid4().hex[:8]

    await ws.send(make_register(ext, name, 1, call_id, branch, tag))
    print(f"[{ext}] REGISTER sent")

    resp = await asyncio.wait_for(ws.recv(), timeout=10)
    print(f"[{ext}] ← {resp.splitlines()[0]}")

    if '401' in resp or '407' in resp:
        www_auth = re.search(r'(?:WWW-Authenticate|Proxy-Authenticate): (.+)', resp).group(1).strip()
        await ws.send(make_register_auth(ext, name, pw, 2, call_id, branch, tag, www_auth))
        print(f"[{ext}] REGISTER+auth sent")
        resp2 = await asyncio.wait_for(ws.recv(), timeout=10)
        print(f"[{ext}] ← {resp2.splitlines()[0]}")
        ok = '200' in resp2
    else:
        ok = '200' in resp

    if ok:
        print(f"[{ext}] ✅ Registered")
    else:
        print(f"[{ext}] ❌ Registration failed")
    return ws, ok

async def main():
    print("=" * 50)
    print("SIP Internal Call Test")
    print(f"  Caller : ext {CALLER['ext']} ({CALLER['name']})")
    print(f"  Callee : ext {CALLEE['ext']} ({CALLEE['name']})")
    print("=" * 50)

    ws_callee, ok2 = await register_ua(CALLEE)
    ws_caller, ok1 = await register_ua(CALLER)

    if not (ok1 and ok2):
        print("Registration failed — aborting"); return

    print("\n--- Initiating INVITE ---")
    inv_call_id = f"{uuid.uuid4().hex}@{DOMAIN}"
    inv_branch  = f"z9hG4bK{uuid.uuid4().hex[:16]}"
    inv_tag     = uuid.uuid4().hex[:8]

    await ws_caller.send(make_invite(CALLER, CALLEE, inv_call_id, inv_branch, inv_tag))
    print(f"[{CALLER['ext']}] INVITE → ext {CALLEE['ext']}")

    answered = asyncio.Event()
    connected = asyncio.Event()

    async def callee_handler():
        while not answered.is_set():
            try:
                msg = await asyncio.wait_for(ws_callee.recv(), timeout=15)
                first = msg.splitlines()[0]
                print(f"[{CALLEE['ext']}] ← {first}")
                if first.startswith('INVITE'):
                    ok_msg = make_200ok(msg, CALLEE['ext'], CALLEE['name'])
                    await ws_callee.send(ok_msg)
                    print(f"[{CALLEE['ext']}] → 200 OK (answered)")
                    answered.set()
            except asyncio.TimeoutError:
                print(f"[{CALLEE['ext']}] Timeout"); break

    async def caller_handler():
        while not connected.is_set():
            try:
                msg = await asyncio.wait_for(ws_caller.recv(), timeout=15)
                first = msg.splitlines()[0]
                print(f"[{CALLER['ext']}] ← {first}")
                if '200 OK' in first or '200' in first:
                    connected.set()
                    print(f"\n🎉 CALL CONNECTED!")
                    print(f"   {CALLER['name']} (ext {CALLER['ext']}) ↔ {CALLEE['name']} (ext {CALLEE['ext']})")
                elif any(c in first for c in ['486','480','404','503','603']):
                    print(f"[{CALLER['ext']}] Call rejected: {first}"); break
            except asyncio.TimeoutError:
                print(f"[{CALLER['ext']}] Timeout"); break

    await asyncio.gather(callee_handler(), caller_handler())
    await asyncio.sleep(3)
    await ws_caller.close()
    await ws_callee.close()

asyncio.run(main())

"""
SIP Internal Call via WebSocket — Priya Kulkarni (5000) calls Kabir Kulkarni (5001).

Key discoveries:
- SIP creds encrypted with AES-CBC, key=SHA-256(VITE_CRYPTO_SECRET), IV=key[:16]
- The bcrypt hash IS the SIP password for MD5 digest auth
- Caller SDP must use RTP/SAVPF (WebRTC) with real STUN-derived srflx candidates
- FreeSWITCH rejects ICE SDPs without real public IP candidates (INCOMPATIBLE_DESTINATION)
- Callee must answer FreeSWITCH's SAVPF/opus offer with matching SAVPF/opus SDP
- All Via headers from the INVITE must be copied into the callee's 200 OK
- Stale registrations cause 480 — deregister (Contact: * Expires: 0) before re-registering
"""
import asyncio, websockets, hashlib, uuid, re, time, json
import socket, struct, random, base64
from Crypto.Cipher import AES

WSS_URL    = "wss://app.qa.unifiedrtc.com/ws/"
DOMAIN     = "1792517466513.unifiedrtc.com"
API        = "https://portal.qa.unifiedrtc.com/api/v1"
KEY_SECRET = "auditcryptosecret0123456789abcde"

# ── helpers ──────────────────────────────────────────────────────────────────

def md5(*parts): return hashlib.md5(":".join(parts).encode()).hexdigest()

def http_post(url, data, headers={}):
    import urllib.request
    body = json.dumps(data).encode()
    h = {"Content-Type":"application/json","User-Agent":"Mozilla/5.0", **headers}
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read())

def http_get(url, headers={}):
    import urllib.request
    h = {"User-Agent":"Mozilla/5.0", **headers}
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read())

def decrypt_sip(enc_b64):
    enc_b64 = enc_b64.replace('-','+').replace('_','/')
    m = 4 - len(enc_b64) % 4
    if m < 4: enc_b64 += "=" * m
    ct  = base64.b64decode(enc_b64)
    key = hashlib.sha256(KEY_SECRET.encode()).digest()
    plain = AES.new(key, AES.MODE_CBC, key[:16]).decrypt(ct)
    pad = plain[-1]; plain = plain[:-pad]
    return json.loads(plain.decode())

def get_sip_creds(email):
    r  = http_post(f"{API}/auth/login", {"email":email,"password":"Aadmin@123456!","device_type":"W"})
    tok = r["data"]["message"]["token"]
    r2 = http_get(f"{API}/sip/get-register-credentials", {"Authorization":f"Bearer {tok}"})
    d  = decrypt_sip(r2["data"]["data"]["sipDetail"])
    return {"ext": str(d["extension"]), "name": d["name"], "pw": d["password"]}

def stun_get_port(stun_host="148.72.141.50", stun_port=3478, local_port=0):
    tx_id = random.getrandbits(96).to_bytes(12, 'big')
    msg   = struct.pack(">HHI", 0x0001, 0, 0x2112A442) + tx_id
    sock  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", local_port)); sock.settimeout(5)
    sock.sendto(msg, (stun_host, stun_port))
    data, _ = sock.recvfrom(1024); sock.close()
    i = 20
    while i < len(data):
        t = struct.unpack(">H", data[i:i+2])[0]; l = struct.unpack(">H", data[i+2:i+4])[0]
        if t == 0x0020:
            xp = struct.unpack(">H", data[i+6:i+8])[0] ^ 0x2112
            xi = struct.unpack(">I", data[i+8:i+12])[0] ^ 0x2112A442
            return socket.inet_ntoa(struct.pack(">I", xi)), xp
        i += 4 + l + (l % 4 != 0)
    return "0.0.0.0", local_port

def extract_all_vias(msg):
    return re.findall(r'^Via: ([^\r\n]+)', msg, re.MULTILINE)

def make_webrtc_sdp(ext, local_ip, ext_ip, ext_port, role="actpass"):
    ts   = int(time.time())
    uf   = uuid.uuid4().hex[:8]; pwd = uuid.uuid4().hex[:24]
    fp   = "4A:AD:B9:B1:3F:82:18:3B:54:02:12:DF:3E:5D:49:6B:19:E5:7C:AB:3E:4A:B7:A3:36:53:9A:C5:88:45:A4:0B"
    ssrc = int(uuid.uuid4().hex[:8], 16)
    return (
        f"v=0\r\no={ext} {ts} {ts} IN IP4 {ext_ip}\r\ns=-\r\nt=0 0\r\n"
        f"a=group:BUNDLE audio\r\n"
        f"a=msid-semantic: WMS stream-{ext}\r\n"
        f"m=audio {ext_port} RTP/SAVPF 102 101\r\nc=IN IP4 {ext_ip}\r\n"
        f"a=rtpmap:102 opus/48000/2\r\n"
        f"a=fmtp:102 useinbandfec=1; maxaveragebitrate=32000; maxplaybackrate=48000; ptime=20; minptime=10; maxptime=40\r\n"
        f"a=rtpmap:101 telephone-event/48000\r\n"
        f"a=fingerprint:sha-256 {fp}\r\na=setup:{role}\r\na=mid:audio\r\na=rtcp-mux\r\n"
        f"a=ice-ufrag:{uf}\r\na=ice-pwd:{pwd}\r\n"
        f"a=candidate:1 1 udp 2130706431 {local_ip} {ext_port} typ host generation 0\r\n"
        f"a=candidate:2 1 udp 1694498815 {ext_ip} {ext_port} typ srflx raddr {local_ip} rport {ext_port} generation 0\r\n"
        f"a=ssrc:{ssrc} cname:stream-{ext}\r\n"
        f"a=msid:stream-{ext} track-{ext}\r\n"
        f"a=silenceSupp:off - - - -\r\na=ptime:20\r\na=sendrecv\r\n"
    )

# ── SIP register (clean: deregister all first) ───────────────────────────────

async def sip_register_clean(u):
    ext = u["ext"]; pw = u["pw"]; name = u["name"]
    ws = await websockets.connect(WSS_URL,
             additional_headers={"Sec-WebSocket-Protocol":"sip"}, subprotocols=["sip"])
    cid = uuid.uuid4().hex + "@" + DOMAIN; tag = uuid.uuid4().hex[:8]
    contact = f"<sip:{ext}@{DOMAIN};transport=ws;+sip.instance=\"<urn:uuid:{uuid.uuid4()}>\">"

    def reg(cseq, br, extra=""):
        return (f"REGISTER sip:{DOMAIN} SIP/2.0\r\nVia: SIP/2.0/WSS {DOMAIN};rport;branch={br};alias\r\n"
                f"From: \"{name}\" <sip:{ext}@{DOMAIN}>;tag={tag}\r\nTo: \"{name}\" <sip:{ext}@{DOMAIN}>\r\n"
                f"Call-ID: {cid}\r\nCSeq: {cseq} REGISTER\r\nContact: {contact}\r\n"
                f"{extra}Expires: 600\r\nContent-Length: 0\r\n\r\n")

    await ws.send(reg(1, "z9hG4bK" + uuid.uuid4().hex[:16]))
    r1    = await asyncio.wait_for(ws.recv(), timeout=8)
    realm = re.search(r'realm="([^"]+)"', r1).group(1)
    nonce = re.search(r'nonce="([^"]+)"', r1).group(1)
    auth  = (f'Digest username="{ext}", realm="{realm}", nonce="{nonce}", uri="sip:{DOMAIN}", '
             f'response="{md5(md5(ext,realm,pw),nonce,md5("REGISTER","sip:"+DOMAIN))}", algorithm=MD5')
    # Deregister all stale contacts
    await ws.send(
        f"REGISTER sip:{DOMAIN} SIP/2.0\r\nVia: SIP/2.0/WSS {DOMAIN};rport;branch=z9hG4bK{uuid.uuid4().hex[:16]};alias\r\n"
        f"From: \"{name}\" <sip:{ext}@{DOMAIN}>;tag={tag}\r\nTo: \"{name}\" <sip:{ext}@{DOMAIN}>\r\n"
        f"Call-ID: {cid}\r\nCSeq: 2 REGISTER\r\nContact: *\r\nAuthorization: {auth}\r\nExpires: 0\r\nContent-Length: 0\r\n\r\n")
    await asyncio.wait_for(ws.recv(), timeout=8)
    await asyncio.sleep(0.3)
    # Fresh registration
    await ws.send(reg(3, "z9hG4bK" + uuid.uuid4().hex[:16]))
    r3    = await asyncio.wait_for(ws.recv(), timeout=8)
    realm2 = re.search(r'realm="([^"]+)"', r3).group(1)
    nonce2 = re.search(r'nonce="([^"]+)"', r3).group(1)
    auth2  = (f'Digest username="{ext}", realm="{realm2}", nonce="{nonce2}", uri="sip:{DOMAIN}", '
              f'response="{md5(md5(ext,realm2,pw),nonce2,md5("REGISTER","sip:"+DOMAIN))}", algorithm=MD5')
    await ws.send(reg(4, "z9hG4bK" + uuid.uuid4().hex[:16], f"Authorization: {auth2}\r\n"))
    r4 = await asyncio.wait_for(ws.recv(), timeout=8)
    ok = "200" in r4.splitlines()[0]
    print(f"[{ext}] Register {'OK' if ok else r4.splitlines()[0]}")
    return ws if ok else None

# ── main ─────────────────────────────────────────────────────────────────────

async def main():
    # Load credentials
    print("Fetching SIP credentials...")
    caller_creds = get_sip_creds("shreyanshdubey1122+4001@gmail.com")  # ext 5000
    callee_creds = get_sip_creds("shreyanshdubey1122+4002@gmail.com")  # ext 5001

    print(f"Caller: {caller_creds['name']} ext {caller_creds['ext']}")
    print(f"Callee: {callee_creds['name']} ext {callee_creds['ext']}")

    # Get STUN reflexive addresses
    print("Getting STUN external addresses...")
    ext_ip, port_c = stun_get_port(local_port=12000)
    _,      port_e = stun_get_port(local_port=12001)
    local_ip = socket.gethostbyname(socket.gethostname())
    print(f"External: {ext_ip}  ports: caller={port_c} callee={port_e}")

    # Register both users (deregister stale contacts first)
    ws_callee = await sip_register_clean(callee_creds)
    ws_caller = await sip_register_clean(caller_creds)
    if not ws_callee or not ws_caller:
        print("Registration failed"); return
    await asyncio.sleep(1)

    ext_c  = caller_creds["ext"]; pw_c   = caller_creds["pw"]; name_c = caller_creds["name"]
    ext_e  = callee_creds["ext"]; name_e = callee_creds["name"]
    cid    = uuid.uuid4().hex + "@" + DOMAIN
    tag    = uuid.uuid4().hex[:8]
    connected = asyncio.Event()

    sdp_offer = make_webrtc_sdp(ext_c, local_ip, ext_ip, port_c, "actpass")

    def inv(cseq, br, extra=""):
        sl = str(len(sdp_offer.encode()))
        return (f"INVITE sip:{ext_e}@{DOMAIN} SIP/2.0\r\nVia: SIP/2.0/WSS {DOMAIN};rport;branch={br};alias\r\n"
                f"From: \"{name_c}\" <sip:{ext_c}@{DOMAIN}>;tag={tag}\r\nTo: \"{name_e}\" <sip:{ext_e}@{DOMAIN}>\r\n"
                f"Call-ID: {cid}\r\nCSeq: {cseq} INVITE\r\nContact: <sip:{ext_c}@{DOMAIN};transport=ws>\r\n"
                f"Max-Forwards: 70\r\n{extra}Content-Type: application/sdp\r\nContent-Length: {sl}\r\n\r\n{sdp_offer}")

    # Callee: listen for incoming INVITE from FreeSWITCH and auto-answer
    async def callee_handler():
        while True:
            try:
                msg = await asyncio.wait_for(ws_callee.recv(), timeout=45)
                fl  = msg.splitlines()[0]; print(f"\n[{ext_e}] <-- {fl}")
                if fl.startswith("INVITE"):
                    vias    = extract_all_vias(msg)
                    call_id = re.search(r'Call-ID: ([^\r\n]+)', msg).group(1)
                    from_h  = re.search(r'From: ([^\r\n]+)', msg).group(1)
                    to_h    = re.search(r'To: ([^\r\n]+)', msg).group(1)
                    cseq_h  = re.search(r'CSeq: ([^\r\n]+)', msg).group(1)
                    to_tag  = uuid.uuid4().hex[:8]
                    sdp_ans = make_webrtc_sdp(ext_e, local_ip, ext_ip, port_e, "active")
                    via_hdrs = "".join(f"Via: {v}\r\n" for v in vias)
                    await ws_callee.send(
                        f"SIP/2.0 200 OK\r\n{via_hdrs}From: {from_h}\r\nTo: {to_h};tag={to_tag}\r\n"
                        f"Call-ID: {call_id}\r\nCSeq: {cseq_h}\r\nContact: <sip:{ext_e}@{DOMAIN};transport=ws>\r\n"
                        f"Content-Type: application/sdp\r\nContent-Length: {len(sdp_ans.encode())}\r\n\r\n{sdp_ans}")
                    print(f"[{ext_e}] --> 200 OK")
                elif fl.startswith("ACK"):
                    print(f"[{ext_e}] ACK — call fully established!"); connected.set(); break
                elif fl.startswith("CANCEL"):
                    vias = extract_all_vias(msg)
                    call_id = re.search(r'Call-ID: ([^\r\n]+)', msg).group(1)
                    from_h  = re.search(r'From: ([^\r\n]+)', msg).group(1)
                    to_h    = re.search(r'To: ([^\r\n]+)', msg).group(1)
                    cseq_h  = re.search(r'CSeq: ([^\r\n]+)', msg).group(1)
                    via_hdrs = "".join(f"Via: {v}\r\n" for v in vias)
                    await ws_callee.send(f"SIP/2.0 200 OK\r\n{via_hdrs}From: {from_h}\r\nTo: {to_h}\r\n"
                                         f"Call-ID: {call_id}\r\nCSeq: {cseq_h}\r\nContent-Length: 0\r\n\r\n")
                    print(f"[{ext_e}] --> 200 OK for CANCEL"); break
            except asyncio.TimeoutError: print(f"[{ext_e}] timeout"); break
            except Exception as e: print(f"[{ext_e}] error: {e}"); break

    # Caller: send INVITE, handle 407, wait for 200 OK
    async def caller_handler():
        br1  = "z9hG4bK" + uuid.uuid4().hex[:16]
        cseq = 1
        print(f"\n[{ext_c}] --> INVITE to ext {ext_e}")
        await ws_caller.send(inv(cseq, br1))
        while True:
            msg = await asyncio.wait_for(ws_caller.recv(), timeout=40)
            fl  = msg.splitlines()[0]; print(f"[{ext_c}] <-- {fl}")
            if "407" in fl:
                chal = re.search(r'Proxy-Authenticate: (.+)', msg).group(1).strip()
                rlm  = re.search(r'realm="([^"]+)"', chal).group(1)
                nnc  = re.search(r'nonce="([^"]+)"', chal).group(1)
                uri2 = f"sip:{ext_e}@{DOMAIN}"
                pa   = (f'Digest username="{ext_c}", realm="{rlm}", nonce="{nnc}", uri="{uri2}", '
                        f'response="{md5(md5(ext_c,rlm,pw_c),nnc,md5("INVITE",uri2))}", algorithm=MD5')
                await ws_caller.send(
                    f"ACK sip:{ext_e}@{DOMAIN} SIP/2.0\r\nVia: SIP/2.0/WSS {DOMAIN};rport;branch={br1};alias\r\n"
                    f"From: \"{name_c}\" <sip:{ext_c}@{DOMAIN}>;tag={tag}\r\nTo: \"{name_e}\" <sip:{ext_e}@{DOMAIN}>\r\n"
                    f"Call-ID: {cid}\r\nCSeq: {cseq} ACK\r\nContent-Length: 0\r\n\r\n")
                cseq += 1
                br2 = "z9hG4bK" + uuid.uuid4().hex[:16]
                await ws_caller.send(inv(cseq, br2, f"Proxy-Authorization: {pa}\r\n"))
                print(f"[{ext_c}] --> INVITE+ProxyAuth (CSeq {cseq})")
            elif "180" in fl:
                print(f"  [{name_e} ringing...]")
            elif "200" in fl and "OK" in fl:
                to_m   = re.search(r'To: [^\r\n]+;tag=([^\r\n;]+)', msg)
                to_tag = to_m.group(1) if to_m else ""
                await ws_caller.send(
                    f"ACK sip:{ext_e}@{DOMAIN} SIP/2.0\r\nVia: SIP/2.0/WSS {DOMAIN};rport;branch=z9hG4bK{uuid.uuid4().hex[:16]};alias\r\n"
                    f"From: \"{name_c}\" <sip:{ext_c}@{DOMAIN}>;tag={tag}\r\nTo: \"{name_e}\" <sip:{ext_e}@{DOMAIN}>;tag={to_tag}\r\n"
                    f"Call-ID: {cid}\r\nCSeq: {cseq} ACK\r\nContent-Length: 0\r\n\r\n")
                print(f"[{ext_c}] --> ACK 200 OK")
                connected.set(); break
            elif any(c in fl for c in ["480","486","488","408","400","403","503","603"]):
                print(f"  Call failed: {fl}")
                reason = re.search(r'Reason: ([^\r\n]+)', msg)
                if reason: print(f"  {reason.group(1)}")
                break

    await asyncio.gather(callee_handler(), caller_handler())

    if connected.is_set():
        print()
        print("=" * 60)
        print("  CALL CONNECTED!")
        print(f"  {name_c} (ext {ext_c}) <--> {name_e} (ext {ext_e})")
        print("=" * 60)
        await asyncio.sleep(5)
        # Hang up
        await ws_caller.send(
            f"BYE sip:{ext_e}@{DOMAIN} SIP/2.0\r\nVia: SIP/2.0/WSS {DOMAIN};rport;branch=z9hG4bK{uuid.uuid4().hex[:16]};alias\r\n"
            f"From: \"{name_c}\" <sip:{ext_c}@{DOMAIN}>;tag={tag}\r\nTo: \"{name_e}\" <sip:{ext_e}@{DOMAIN}>\r\n"
            f"Call-ID: {cid}\r\nCSeq: 10 BYE\r\nContent-Length: 0\r\n\r\n")
        print(f"[{ext_c}] --> BYE (call ended after 5s)")

    await asyncio.sleep(1)
    await ws_caller.close()
    await ws_callee.close()


asyncio.run(main())

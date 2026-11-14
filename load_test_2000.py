"""
Load test: first 2000 members → 1000 concurrent SIP calls × 300s (5 min).
Email suffixes +2..+2001, extensions 1001..3000.
"""
import asyncio, websockets, hashlib, uuid, re, time, json
import socket, struct, random, base64, sys
from Crypto.Cipher import AES
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request, urllib.error

WSS_URL    = "wss://app.qa.unifiedrtc.com/ws/"
DOMAIN     = "1792517466513.unifiedrtc.com"
API        = "https://portal.qa.unifiedrtc.com/api/v1"
KEY_SECRET = "auditcryptosecret0123456789abcde"

TOTAL_USERS  = 400
HOLD_SECONDS = 300
CRED_WORKERS = 5
REG_SEM      = 30
CALL_SEM     = 100
CREDS_CACHE  = "/tmp/lt_creds_cache.json"

# ── helpers ──────────────────────────────────────────────────────────────────
def md5(*parts): return hashlib.md5(":".join(parts).encode()).hexdigest()

def http_post(url, data):
    body = json.dumps(data).encode()
    h = {"Content-Type":"application/json","User-Agent":"Mozilla/5.0"}
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r: return json.loads(r.read())

def http_get(url, headers={}):
    h = {"User-Agent":"Mozilla/5.0", **headers}
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r: return json.loads(r.read())

def decrypt_sip(enc_b64):
    enc_b64 = enc_b64.replace('-','+').replace('_','/')
    m = 4 - len(enc_b64) % 4
    if m < 4: enc_b64 += "=" * m
    ct  = base64.b64decode(enc_b64)
    key = hashlib.sha256(KEY_SECRET.encode()).digest()
    plain = AES.new(key, AES.MODE_CBC, key[:16]).decrypt(ct)
    pad = plain[-1]; return json.loads(plain[:-pad].decode())

def fetch_one(suffix):
    time.sleep(1)  # 5 workers × 1s = ~5 req/s, stays under rate limit
    email = f"shreyanshdubey1122+{suffix}@gmail.com"
    for attempt in range(5):
        try:
            r = http_post(f"{API}/auth/login",
                          {"email":email,"password":"Aadmin@123456!","device_type":"W"})
            tok = r["data"]["message"]["token"]
            r2 = http_get(f"{API}/sip/get-register-credentials",
                          {"Authorization":f"Bearer {tok}"})
            d = decrypt_sip(r2["data"]["data"]["sipDetail"])
            return {"suffix":suffix,"ext":str(d["extension"]),"name":d["name"],"pw":d["password"]}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (attempt+1))  # back off on rate limit
            else:
                time.sleep(1)
        except Exception:
            time.sleep(2)
    return None

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
        f"a=group:BUNDLE audio\r\na=msid-semantic: WMS stream-{ext}\r\n"
        f"m=audio {ext_port} RTP/SAVPF 102 101\r\nc=IN IP4 {ext_ip}\r\n"
        f"a=rtpmap:102 opus/48000/2\r\n"
        f"a=fmtp:102 useinbandfec=1; maxaveragebitrate=32000; maxplaybackrate=48000; ptime=20; minptime=10; maxptime=40\r\n"
        f"a=rtpmap:101 telephone-event/48000\r\n"
        f"a=fingerprint:sha-256 {fp}\r\na=setup:{role}\r\na=mid:audio\r\na=rtcp-mux\r\n"
        f"a=ice-ufrag:{uf}\r\na=ice-pwd:{pwd}\r\n"
        f"a=candidate:1 1 udp 2130706431 {local_ip} {ext_port} typ host generation 0\r\n"
        f"a=candidate:2 1 udp 1694498815 {ext_ip} {ext_port} typ srflx raddr {local_ip} rport {ext_port} generation 0\r\n"
        f"a=ssrc:{ssrc} cname:stream-{ext}\r\na=msid:stream-{ext} track-{ext}\r\n"
        f"a=silenceSupp:off - - - -\r\na=ptime:20\r\na=sendrecv\r\n"
    )

# ── SIP register ──────────────────────────────────────────────────────────────
async def sip_register_clean(u, reg_sem):
    async with reg_sem:
        ext = u["ext"]; pw = u["pw"]; name = u["name"]
        try:
            ws = await asyncio.wait_for(
                websockets.connect(WSS_URL,
                    additional_headers={"Sec-WebSocket-Protocol":"sip"},
                    subprotocols=["sip"], ping_interval=20, ping_timeout=30),
                timeout=15)
        except Exception as e:
            return None, str(e)

        cid = uuid.uuid4().hex + "@" + DOMAIN; tag = uuid.uuid4().hex[:8]
        contact = f"<sip:{ext}@{DOMAIN};transport=ws;+sip.instance=\"<urn:uuid:{uuid.uuid4()}>\">"

        def reg(cseq, br, extra=""):
            return (f"REGISTER sip:{DOMAIN} SIP/2.0\r\n"
                    f"Via: SIP/2.0/WSS {DOMAIN};rport;branch={br};alias\r\n"
                    f"From: \"{name}\" <sip:{ext}@{DOMAIN}>;tag={tag}\r\n"
                    f"To: \"{name}\" <sip:{ext}@{DOMAIN}>\r\n"
                    f"Call-ID: {cid}\r\nCSeq: {cseq} REGISTER\r\nContact: {contact}\r\n"
                    f"{extra}Expires: 600\r\nContent-Length: 0\r\n\r\n")
        try:
            await ws.send(reg(1, "z9hG4bK" + uuid.uuid4().hex[:16]))
            r1 = await asyncio.wait_for(ws.recv(), timeout=10)
            realm = re.search(r'realm="([^"]+)"', r1).group(1)
            nonce = re.search(r'nonce="([^"]+)"', r1).group(1)
            auth = (f'Digest username="{ext}", realm="{realm}", nonce="{nonce}", uri="sip:{DOMAIN}", '
                    f'response="{md5(md5(ext,realm,pw),nonce,md5("REGISTER","sip:"+DOMAIN))}", algorithm=MD5')
            await ws.send(
                f"REGISTER sip:{DOMAIN} SIP/2.0\r\n"
                f"Via: SIP/2.0/WSS {DOMAIN};rport;branch=z9hG4bK{uuid.uuid4().hex[:16]};alias\r\n"
                f"From: \"{name}\" <sip:{ext}@{DOMAIN}>;tag={tag}\r\n"
                f"To: \"{name}\" <sip:{ext}@{DOMAIN}>\r\n"
                f"Call-ID: {cid}\r\nCSeq: 2 REGISTER\r\nContact: *\r\n"
                f"Authorization: {auth}\r\nExpires: 0\r\nContent-Length: 0\r\n\r\n")
            await asyncio.wait_for(ws.recv(), timeout=10)
            await asyncio.sleep(0.2)
            await ws.send(reg(3, "z9hG4bK" + uuid.uuid4().hex[:16]))
            r3 = await asyncio.wait_for(ws.recv(), timeout=10)
            realm2 = re.search(r'realm="([^"]+)"', r3).group(1)
            nonce2 = re.search(r'nonce="([^"]+)"', r3).group(1)
            auth2 = (f'Digest username="{ext}", realm="{realm2}", nonce="{nonce2}", uri="sip:{DOMAIN}", '
                     f'response="{md5(md5(ext,realm2,pw),nonce2,md5("REGISTER","sip:"+DOMAIN))}", algorithm=MD5')
            await ws.send(reg(4, "z9hG4bK" + uuid.uuid4().hex[:16], f"Authorization: {auth2}\r\n"))
            r4 = await asyncio.wait_for(ws.recv(), timeout=10)
            if "200" in r4.splitlines()[0]:
                return ws, None
            return None, r4.splitlines()[0]
        except Exception as e:
            try: await ws.close()
            except: pass
            return None, str(e)

# ── single call pair ──────────────────────────────────────────────────────────
async def run_call_pair(pair_id, caller, callee, ws_caller, ws_callee,
                        local_ip, ext_ip, call_sem, stats):
    port_c = 13000 + (pair_id * 2) % 30000
    port_e = 13001 + (pair_id * 2) % 30000
    async with call_sem:
        ext_c = caller["ext"]; pw_c = caller["pw"]; name_c = caller["name"]
        ext_e = callee["ext"]; name_e = callee["name"]
        cid   = uuid.uuid4().hex + "@" + DOMAIN
        tag   = uuid.uuid4().hex[:8]
        connected = asyncio.Event()
        sdp_offer = make_webrtc_sdp(ext_c, local_ip, ext_ip, port_c, "actpass")

        def inv(cseq, br, extra=""):
            sl = str(len(sdp_offer.encode()))
            return (f"INVITE sip:{ext_e}@{DOMAIN} SIP/2.0\r\n"
                    f"Via: SIP/2.0/WSS {DOMAIN};rport;branch={br};alias\r\n"
                    f"From: \"{name_c}\" <sip:{ext_c}@{DOMAIN}>;tag={tag}\r\n"
                    f"To: \"{name_e}\" <sip:{ext_e}@{DOMAIN}>\r\n"
                    f"Call-ID: {cid}\r\nCSeq: {cseq} INVITE\r\n"
                    f"Contact: <sip:{ext_c}@{DOMAIN};transport=ws>\r\n"
                    f"Max-Forwards: 70\r\n{extra}"
                    f"Content-Type: application/sdp\r\nContent-Length: {sl}\r\n\r\n{sdp_offer}")

        async def callee_handler():
            while True:
                try:
                    msg = await asyncio.wait_for(ws_callee.recv(), timeout=45)
                    fl  = msg.splitlines()[0]
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
                            f"Call-ID: {call_id}\r\nCSeq: {cseq_h}\r\n"
                            f"Contact: <sip:{ext_e}@{DOMAIN};transport=ws>\r\n"
                            f"Content-Type: application/sdp\r\nContent-Length: {len(sdp_ans.encode())}\r\n\r\n{sdp_ans}")
                    elif fl.startswith("ACK"):
                        connected.set(); break
                    elif fl.startswith("CANCEL"):
                        vias    = extract_all_vias(msg)
                        call_id = re.search(r'Call-ID: ([^\r\n]+)', msg).group(1)
                        from_h  = re.search(r'From: ([^\r\n]+)', msg).group(1)
                        to_h    = re.search(r'To: ([^\r\n]+)', msg).group(1)
                        cseq_h  = re.search(r'CSeq: ([^\r\n]+)', msg).group(1)
                        via_hdrs = "".join(f"Via: {v}\r\n" for v in vias)
                        await ws_callee.send(
                            f"SIP/2.0 200 OK\r\n{via_hdrs}From: {from_h}\r\nTo: {to_h}\r\n"
                            f"Call-ID: {call_id}\r\nCSeq: {cseq_h}\r\nContent-Length: 0\r\n\r\n")
                        break
                except: break

        async def caller_handler():
            br1 = "z9hG4bK" + uuid.uuid4().hex[:16]; cseq = 1
            await ws_caller.send(inv(cseq, br1))
            while True:
                try:
                    msg = await asyncio.wait_for(ws_caller.recv(), timeout=40)
                    fl  = msg.splitlines()[0]
                    if "407" in fl:
                        chal = re.search(r'Proxy-Authenticate: (.+)', msg).group(1).strip()
                        rlm  = re.search(r'realm="([^"]+)"', chal).group(1)
                        nnc  = re.search(r'nonce="([^"]+)"', chal).group(1)
                        uri2 = f"sip:{ext_e}@{DOMAIN}"
                        pa   = (f'Digest username="{ext_c}", realm="{rlm}", nonce="{nnc}", uri="{uri2}", '
                                f'response="{md5(md5(ext_c,rlm,pw_c),nnc,md5("INVITE",uri2))}", algorithm=MD5')
                        await ws_caller.send(
                            f"ACK sip:{ext_e}@{DOMAIN} SIP/2.0\r\n"
                            f"Via: SIP/2.0/WSS {DOMAIN};rport;branch={br1};alias\r\n"
                            f"From: \"{name_c}\" <sip:{ext_c}@{DOMAIN}>;tag={tag}\r\n"
                            f"To: \"{name_e}\" <sip:{ext_e}@{DOMAIN}>\r\n"
                            f"Call-ID: {cid}\r\nCSeq: {cseq} ACK\r\nContent-Length: 0\r\n\r\n")
                        cseq += 1; br2 = "z9hG4bK" + uuid.uuid4().hex[:16]
                        await ws_caller.send(inv(cseq, br2, f"Proxy-Authorization: {pa}\r\n"))
                    elif "200" in fl and "OK" in fl:
                        to_m   = re.search(r'To: [^\r\n]+;tag=([^\r\n;]+)', msg)
                        to_tag = to_m.group(1) if to_m else ""
                        await ws_caller.send(
                            f"ACK sip:{ext_e}@{DOMAIN} SIP/2.0\r\n"
                            f"Via: SIP/2.0/WSS {DOMAIN};rport;branch=z9hG4bK{uuid.uuid4().hex[:16]};alias\r\n"
                            f"From: \"{name_c}\" <sip:{ext_c}@{DOMAIN}>;tag={tag}\r\n"
                            f"To: \"{name_e}\" <sip:{ext_e}@{DOMAIN}>;tag={to_tag}\r\n"
                            f"Call-ID: {cid}\r\nCSeq: {cseq} ACK\r\nContent-Length: 0\r\n\r\n")
                        connected.set(); break
                    elif any(c in fl for c in ["480","486","488","408","400","403","503","603"]):
                        break
                except: break

        try:
            await asyncio.gather(callee_handler(), caller_handler())
        except: pass

        if connected.is_set():
            stats["connected"] += 1
            n = stats["connected"]
            if n % 100 == 0 or n <= 5:
                print(f"  [{n} connected] pair-{pair_id:04d}: {name_c}({ext_c}) <-> {name_e}({ext_e})", flush=True)
            await asyncio.sleep(HOLD_SECONDS)
            try:
                await ws_caller.send(
                    f"BYE sip:{ext_e}@{DOMAIN} SIP/2.0\r\n"
                    f"Via: SIP/2.0/WSS {DOMAIN};rport;branch=z9hG4bK{uuid.uuid4().hex[:16]};alias\r\n"
                    f"From: \"{name_c}\" <sip:{ext_c}@{DOMAIN}>;tag={tag}\r\n"
                    f"To: \"{name_e}\" <sip:{ext_e}@{DOMAIN}>\r\n"
                    f"Call-ID: {cid}\r\nCSeq: 10 BYE\r\nContent-Length: 0\r\n\r\n")
            except: pass
            stats["completed"] += 1
        else:
            stats["failed"] += 1
            if stats["failed"] <= 10:
                print(f"  [FAIL] pair-{pair_id:04d}: {name_c}({ext_c}) -> {name_e}({ext_e})", flush=True)

        try: await ws_caller.close()
        except: pass
        try: await ws_callee.close()
        except: pass

# ── main ──────────────────────────────────────────────────────────────────────
async def main():
    t0 = time.time()
    print("=" * 65, flush=True)
    print(f"  LOAD TEST: {TOTAL_USERS} members → {TOTAL_USERS//2} calls × {HOLD_SECONDS}s (5 min)", flush=True)
    print("=" * 65, flush=True)

    # ── Step 1: fetch credentials (with cache) ────────────────────────────
    import os
    cache_age = time.time() - os.path.getmtime(CREDS_CACHE) if os.path.exists(CREDS_CACHE) else 9999
    if cache_age < 1800:
        print(f"\n[1/4] Loading cached creds ({cache_age:.0f}s old)...", flush=True)
        with open(CREDS_CACHE) as f: users = json.load(f)
        print(f"  → {len(users)} creds loaded from cache", flush=True)
    else:
        print(f"\n[1/4] Fetching creds ({TOTAL_USERS} users, {CRED_WORKERS} workers)...", flush=True)
        suffixes = list(range(402, 402 + TOTAL_USERS))  # fresh range, avoid stale contacts
        users = []
        done = 0; errors = 0
        with ThreadPoolExecutor(max_workers=CRED_WORKERS) as pool:
            futs = {pool.submit(fetch_one, s): s for s in suffixes}
            for fut in as_completed(futs):
                done += 1
                r = fut.result()
                if r: users.append(r)
                else: errors += 1
                if done % 100 == 0:
                    elapsed = time.time()-t0
                    rate = done/elapsed if elapsed > 0 else 1
                    eta = (TOTAL_USERS-done)/rate
                    print(f"  {done}/{TOTAL_USERS} | OK:{len(users)} err:{errors} | {rate:.1f}/s ETA:{eta:.0f}s", flush=True)
        users.sort(key=lambda u: int(u["ext"]))
        with open(CREDS_CACHE, "w") as f: json.dump(users, f)
        print(f"  → {len(users)} creds fetched ({errors} failed) in {time.time()-t0:.0f}s (cached)", flush=True)
    if len(users) < 2:
        print("Too few users, aborting."); return

    # ── Step 2: STUN ──────────────────────────────────────────────────────
    print("\n[2/4] STUN external IP...", flush=True)
    ext_ip, _ = stun_get_port()
    local_ip  = socket.gethostbyname(socket.gethostname())
    print(f"  ext={ext_ip}  local={local_ip}", flush=True)

    # ── Step 3: register ──────────────────────────────────────────────────
    print(f"\n[3/4] Registering {len(users)} users (sem={REG_SEM})...", flush=True)
    reg_sem = asyncio.Semaphore(REG_SEM)

    async def reg_indexed(i, u):
        ws, err = await sip_register_clean(u, reg_sem)
        return i, ws, err

    reg_results = await asyncio.gather(*[reg_indexed(i, u) for i, u in enumerate(users)])
    ws_by_idx = {}
    reg_ok = 0
    for idx, ws, err in reg_results:
        if ws:
            ws_by_idx[idx] = ws; reg_ok += 1
    print(f"  → {reg_ok}/{len(users)} registered OK", flush=True)
    if reg_ok < 2:
        print("Too few registrations, aborting."); return

    # ── Step 4: calls ─────────────────────────────────────────────────────
    pairs_count = sum(1 for i in range(0, len(users)-1, 2) if i in ws_by_idx and (i+1) in ws_by_idx)
    print(f"\n[4/4] Launching {pairs_count} calls (hold={HOLD_SECONDS}s)...", flush=True)
    stats = {"connected":0, "failed":0, "completed":0}
    call_sem = asyncio.Semaphore(CALL_SEM)
    call_tasks = []
    pair_id = 0
    for i in range(0, len(users)-1, 2):
        if i in ws_by_idx and (i+1) in ws_by_idx:
            call_tasks.append(run_call_pair(pair_id, users[i], users[i+1],
                                            ws_by_idx[i], ws_by_idx[i+1],
                                            local_ip, ext_ip, call_sem, stats))
            pair_id += 1

    t_calls = time.time()
    await asyncio.gather(*call_tasks, return_exceptions=True)

    print(flush=True)
    print("=" * 65, flush=True)
    print(f"  DONE", flush=True)
    print(f"  Pairs attempted : {pair_id}", flush=True)
    print(f"  Connected       : {stats['connected']}", flush=True)
    print(f"  Failed          : {stats['failed']}", flush=True)
    print(f"  Completed (BYE) : {stats['completed']}", flush=True)
    print(f"  Call duration   : {time.time()-t_calls:.1f}s", flush=True)
    print(f"  Total wall-clock: {time.time()-t0:.1f}s", flush=True)
    print("=" * 65, flush=True)

asyncio.run(main())

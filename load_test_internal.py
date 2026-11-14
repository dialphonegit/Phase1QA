"""
Internal SIP call load test — platform.qa.unifiedrtc.com

Dials N concurrent call pairs (caller → callee) using WebSocket SIP.
All traffic stays on-platform (no PSTN, no DID required).

Usage:
  python3 load_test_internal.py [--pairs N] [--hold S] [--suffix-start X]

Defaults: 50 pairs, 60s hold, suffix start 2 (accounts +2/+3, +4/+5, …)
"""
import asyncio, websockets, hashlib, uuid, re, time, json
import socket, struct, random, base64, argparse, sys
import urllib.request, urllib.error
from Crypto.Cipher import AES
from concurrent.futures import ThreadPoolExecutor, as_completed

WSS_URL    = "wss://app.qa.unifiedrtc.com/ws/"
DOMAIN     = "1792517466513.unifiedrtc.com"
API        = "https://portal.qa.unifiedrtc.com/api/v1"
KEY_SECRET = "auditcryptosecret0123456789abcde"
CREDS_CACHE = "/tmp/ilt_creds_cache.json"

# ── helpers ───────────────────────────────────────────────────────────────────

def md5(*parts):
    return hashlib.md5(":".join(parts).encode()).hexdigest()

def http_post(url, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body,
          headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def http_get(url, tok):
    req = urllib.request.Request(url,
          headers={"Authorization": f"Bearer {tok}", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def decrypt_sip(enc_b64):
    enc_b64 = enc_b64.replace("-", "+").replace("_", "/")
    pad = 4 - len(enc_b64) % 4
    if pad < 4:
        enc_b64 += "=" * pad
    ct  = base64.b64decode(enc_b64)
    key = hashlib.sha256(KEY_SECRET.encode()).digest()
    plain = AES.new(key, AES.MODE_CBC, key[:16]).decrypt(ct)
    return json.loads(plain[: -plain[-1]].decode())

def fetch_creds(suffix):
    time.sleep(1.5)  # ~2 workers × 1.5s = ~1.3 req/s, stays under rate limit
    email = f"shreyanshdubey1122+{suffix}@gmail.com"
    for attempt in range(4):
        try:
            r  = http_post(f"{API}/auth/login",
                           {"email": email, "password": "Aadmin@123456!", "device_type": "W"})
            tok = r["data"]["message"]["token"]
            r2 = http_get(f"{API}/sip/get-register-credentials", tok)
            d  = decrypt_sip(r2["data"]["data"]["sipDetail"])
            return {"suffix": suffix, "ext": str(d["extension"]),
                    "name": d["name"], "pw": d["password"]}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
            else:
                time.sleep(1)
        except Exception:
            time.sleep(1.5)
    return None

def stun_get_ip():
    tx_id = random.getrandbits(96).to_bytes(12, "big")
    msg   = struct.pack(">HHI", 0x0001, 0, 0x2112A442) + tx_id
    sock  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    sock.sendto(msg, ("148.72.141.50", 3478))
    data, _ = sock.recvfrom(1024)
    sock.close()
    i = 20
    while i < len(data):
        t = struct.unpack(">H", data[i:i+2])[0]
        l = struct.unpack(">H", data[i+2:i+4])[0]
        if t == 0x0020:
            xi = struct.unpack(">I", data[i+8:i+12])[0] ^ 0x2112A442
            return socket.inet_ntoa(struct.pack(">I", xi))
        i += 4 + l + (l % 4 != 0)
    return "0.0.0.0"

def vias(msg):
    return re.findall(r"^Via: ([^\r\n]+)", msg, re.MULTILINE)

def sdp(ext, lip, eip, port, role="actpass"):
    ts   = int(time.time())
    uf   = uuid.uuid4().hex[:8]
    pw   = uuid.uuid4().hex[:24]
    ssrc = int(uuid.uuid4().hex[:8], 16)
    fp   = "4A:AD:B9:B1:3F:82:18:3B:54:02:12:DF:3E:5D:49:6B:19:E5:7C:AB:3E:4A:B7:A3:36:53:9A:C5:88:45:A4:0B"
    return (
        f"v=0\r\no={ext} {ts} {ts} IN IP4 {eip}\r\ns=-\r\nt=0 0\r\n"
        f"a=group:BUNDLE audio\r\na=msid-semantic: WMS stream-{ext}\r\n"
        f"m=audio {port} RTP/SAVPF 102 101\r\nc=IN IP4 {eip}\r\n"
        f"a=rtpmap:102 opus/48000/2\r\n"
        f"a=fmtp:102 useinbandfec=1; maxaveragebitrate=32000; maxplaybackrate=48000; ptime=20; minptime=10; maxptime=40\r\n"
        f"a=rtpmap:101 telephone-event/48000\r\n"
        f"a=fingerprint:sha-256 {fp}\r\na=setup:{role}\r\na=mid:audio\r\na=rtcp-mux\r\n"
        f"a=ice-ufrag:{uf}\r\na=ice-pwd:{pw}\r\n"
        f"a=candidate:1 1 udp 2130706431 {lip} {port} typ host generation 0\r\n"
        f"a=candidate:2 1 udp 1694498815 {eip} {port} typ srflx raddr {lip} rport {port} generation 0\r\n"
        f"a=ssrc:{ssrc} cname:stream-{ext}\r\na=msid:stream-{ext} track-{ext}\r\n"
        f"a=silenceSupp:off - - - -\r\na=ptime:20\r\na=sendrecv\r\n"
    )

# ── SIP register ──────────────────────────────────────────────────────────────

async def register(u, sem):
    async with sem:
        ext, pw, name = u["ext"], u["pw"], u["name"]
        try:
            ws = await asyncio.wait_for(
                websockets.connect(WSS_URL,
                    additional_headers={"Sec-WebSocket-Protocol": "sip"},
                    subprotocols=["sip"], ping_interval=20, ping_timeout=30),
                timeout=15)
        except Exception as e:
            return None, str(e)

        cid = uuid.uuid4().hex + "@" + DOMAIN
        tag = uuid.uuid4().hex[:8]
        contact = f'<sip:{ext}@{DOMAIN};transport=ws;+sip.instance="<urn:uuid:{uuid.uuid4()}>">'

        def reg_msg(cseq, br, extra=""):
            return (f"REGISTER sip:{DOMAIN} SIP/2.0\r\n"
                    f"Via: SIP/2.0/WSS {DOMAIN};rport;branch={br};alias\r\n"
                    f"From: \"{name}\" <sip:{ext}@{DOMAIN}>;tag={tag}\r\n"
                    f"To: \"{name}\" <sip:{ext}@{DOMAIN}>\r\n"
                    f"Call-ID: {cid}\r\nCSeq: {cseq} REGISTER\r\nContact: {contact}\r\n"
                    f"{extra}Expires: 600\r\nContent-Length: 0\r\n\r\n")

        try:
            await ws.send(reg_msg(1, "z9hG4bK" + uuid.uuid4().hex[:16]))
            r1 = await asyncio.wait_for(ws.recv(), timeout=10)
            realm = re.search(r'realm="([^"]+)"', r1).group(1)
            nonce = re.search(r'nonce="([^"]+)"', r1).group(1)
            auth = (f'Digest username="{ext}", realm="{realm}", nonce="{nonce}", uri="sip:{DOMAIN}", '
                    f'response="{md5(md5(ext,realm,pw),nonce,md5("REGISTER","sip:"+DOMAIN))}", algorithm=MD5')
            # deregister stale contacts
            await ws.send(
                f"REGISTER sip:{DOMAIN} SIP/2.0\r\n"
                f"Via: SIP/2.0/WSS {DOMAIN};rport;branch=z9hG4bK{uuid.uuid4().hex[:16]};alias\r\n"
                f"From: \"{name}\" <sip:{ext}@{DOMAIN}>;tag={tag}\r\n"
                f"To: \"{name}\" <sip:{ext}@{DOMAIN}>\r\n"
                f"Call-ID: {cid}\r\nCSeq: 2 REGISTER\r\nContact: *\r\n"
                f"Authorization: {auth}\r\nExpires: 0\r\nContent-Length: 0\r\n\r\n")
            await asyncio.wait_for(ws.recv(), timeout=10)
            await asyncio.sleep(0.2)
            # fresh registration
            await ws.send(reg_msg(3, "z9hG4bK" + uuid.uuid4().hex[:16]))
            r3 = await asyncio.wait_for(ws.recv(), timeout=10)
            realm2 = re.search(r'realm="([^"]+)"', r3).group(1)
            nonce2 = re.search(r'nonce="([^"]+)"', r3).group(1)
            auth2 = (f'Digest username="{ext}", realm="{realm2}", nonce="{nonce2}", uri="sip:{DOMAIN}", '
                     f'response="{md5(md5(ext,realm2,pw),nonce2,md5("REGISTER","sip:"+DOMAIN))}", algorithm=MD5')
            await ws.send(reg_msg(4, "z9hG4bK" + uuid.uuid4().hex[:16], f"Authorization: {auth2}\r\n"))
            r4 = await asyncio.wait_for(ws.recv(), timeout=10)
            if "200" in r4.splitlines()[0]:
                return ws, None
            return None, r4.splitlines()[0]
        except Exception as e:
            try: await ws.close()
            except: pass
            return None, str(e)

# ── single call pair ──────────────────────────────────────────────────────────

async def call_pair(pid, caller, callee, ws_c, ws_e, lip, eip, hold, sem, stats):
    async with sem:
        ext_c, pw_c, name_c = caller["ext"], caller["pw"], caller["name"]
        ext_e, name_e       = callee["ext"], callee["name"]
        port_c = 14000 + (pid * 2    ) % 28000
        port_e = 14001 + (pid * 2 + 1) % 28000
        cid    = uuid.uuid4().hex + "@" + DOMAIN
        tag    = uuid.uuid4().hex[:8]
        ok     = asyncio.Event()
        sdp_c  = sdp(ext_c, lip, eip, port_c, "actpass")

        def invite(cseq, br, extra=""):
            sl = len(sdp_c.encode())
            return (f"INVITE sip:{ext_e}@{DOMAIN} SIP/2.0\r\n"
                    f"Via: SIP/2.0/WSS {DOMAIN};rport;branch={br};alias\r\n"
                    f"From: \"{name_c}\" <sip:{ext_c}@{DOMAIN}>;tag={tag}\r\n"
                    f"To: \"{name_e}\" <sip:{ext_e}@{DOMAIN}>\r\n"
                    f"Call-ID: {cid}\r\nCSeq: {cseq} INVITE\r\n"
                    f"Contact: <sip:{ext_c}@{DOMAIN};transport=ws>\r\n"
                    f"Max-Forwards: 70\r\n{extra}"
                    f"Content-Type: application/sdp\r\nContent-Length: {sl}\r\n\r\n{sdp_c}")

        async def on_callee():
            while True:
                try:
                    msg = await asyncio.wait_for(ws_e.recv(), timeout=45)
                    fl  = msg.splitlines()[0]
                    if fl.startswith("INVITE"):
                        via_h  = "".join(f"Via: {v}\r\n" for v in vias(msg))
                        cid_h  = re.search(r"Call-ID: ([^\r\n]+)", msg).group(1)
                        from_h = re.search(r"From: ([^\r\n]+)", msg).group(1)
                        to_h   = re.search(r"To: ([^\r\n]+)", msg).group(1)
                        cseq_h = re.search(r"CSeq: ([^\r\n]+)", msg).group(1)
                        t_tag  = uuid.uuid4().hex[:8]
                        sdp_e  = sdp(ext_e, lip, eip, port_e, "active")
                        await ws_e.send(
                            f"SIP/2.0 200 OK\r\n{via_h}From: {from_h}\r\nTo: {to_h};tag={t_tag}\r\n"
                            f"Call-ID: {cid_h}\r\nCSeq: {cseq_h}\r\n"
                            f"Contact: <sip:{ext_e}@{DOMAIN};transport=ws>\r\n"
                            f"Content-Type: application/sdp\r\nContent-Length: {len(sdp_e.encode())}\r\n\r\n{sdp_e}")
                    elif fl.startswith("ACK"):
                        ok.set(); break
                    elif fl.startswith("CANCEL"):
                        via_h  = "".join(f"Via: {v}\r\n" for v in vias(msg))
                        cid_h  = re.search(r"Call-ID: ([^\r\n]+)", msg).group(1)
                        from_h = re.search(r"From: ([^\r\n]+)", msg).group(1)
                        to_h   = re.search(r"To: ([^\r\n]+)", msg).group(1)
                        cseq_h = re.search(r"CSeq: ([^\r\n]+)", msg).group(1)
                        await ws_e.send(
                            f"SIP/2.0 200 OK\r\n{via_h}From: {from_h}\r\nTo: {to_h}\r\n"
                            f"Call-ID: {cid_h}\r\nCSeq: {cseq_h}\r\nContent-Length: 0\r\n\r\n")
                        break
                except: break

        async def on_caller():
            br1 = "z9hG4bK" + uuid.uuid4().hex[:16]; cseq = 1
            await ws_c.send(invite(cseq, br1))
            while True:
                try:
                    msg = await asyncio.wait_for(ws_c.recv(), timeout=40)
                    fl  = msg.splitlines()[0]
                    if "407" in fl:
                        chal = re.search(r"Proxy-Authenticate: (.+)", msg).group(1).strip()
                        rlm  = re.search(r'realm="([^"]+)"', chal).group(1)
                        nnc  = re.search(r'nonce="([^"]+)"', chal).group(1)
                        uri2 = f"sip:{ext_e}@{DOMAIN}"
                        pa   = (f'Digest username="{ext_c}", realm="{rlm}", nonce="{nnc}", uri="{uri2}", '
                                f'response="{md5(md5(ext_c,rlm,pw_c),nnc,md5("INVITE",uri2))}", algorithm=MD5')
                        await ws_c.send(
                            f"ACK sip:{ext_e}@{DOMAIN} SIP/2.0\r\n"
                            f"Via: SIP/2.0/WSS {DOMAIN};rport;branch={br1};alias\r\n"
                            f"From: \"{name_c}\" <sip:{ext_c}@{DOMAIN}>;tag={tag}\r\n"
                            f"To: \"{name_e}\" <sip:{ext_e}@{DOMAIN}>\r\n"
                            f"Call-ID: {cid}\r\nCSeq: {cseq} ACK\r\nContent-Length: 0\r\n\r\n")
                        cseq += 1
                        await ws_c.send(invite(cseq, "z9hG4bK"+uuid.uuid4().hex[:16], f"Proxy-Authorization: {pa}\r\n"))
                    elif "200" in fl and "OK" in fl:
                        t_m = re.search(r"To: [^\r\n]+;tag=([^\r\n;]+)", msg)
                        t_t = t_m.group(1) if t_m else ""
                        await ws_c.send(
                            f"ACK sip:{ext_e}@{DOMAIN} SIP/2.0\r\n"
                            f"Via: SIP/2.0/WSS {DOMAIN};rport;branch=z9hG4bK{uuid.uuid4().hex[:16]};alias\r\n"
                            f"From: \"{name_c}\" <sip:{ext_c}@{DOMAIN}>;tag={tag}\r\n"
                            f"To: \"{name_e}\" <sip:{ext_e}@{DOMAIN}>;tag={t_t}\r\n"
                            f"Call-ID: {cid}\r\nCSeq: {cseq} ACK\r\nContent-Length: 0\r\n\r\n")
                        ok.set(); break
                    elif any(c in fl for c in ["480","486","488","408","400","403","503","603"]):
                        break
                except: break

        try:
            await asyncio.gather(on_callee(), on_caller())
        except: pass

        if ok.is_set():
            stats["connected"] += 1
            n = stats["connected"]
            if n <= 5 or n % 50 == 0:
                print(f"  [{n:4d} up] pair-{pid:04d}: {name_c}({ext_c}) ↔ {name_e}({ext_e})", flush=True)
            await asyncio.sleep(hold)
            try:
                await ws_c.send(
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
                print(f"  [FAIL] pair-{pid:04d}: {name_c}({ext_c}) -> {name_e}({ext_e})", flush=True)

        for ws in (ws_c, ws_e):
            try: await ws.close()
            except: pass

# ── main ──────────────────────────────────────────────────────────────────────

async def main(pairs, hold, suffix_start, cred_workers, reg_sem_n, call_sem_n):
    t0 = time.time()
    total_users = pairs * 2
    print("=" * 70, flush=True)
    print(f"  INTERNAL LOAD TEST", flush=True)
    print(f"  {pairs} pairs × {hold}s hold  |  suffixes +{suffix_start}..+{suffix_start+total_users-1}", flush=True)
    print("=" * 70, flush=True)

    # ── 1. credentials ────────────────────────────────────────────────────
    import os
    cache_age = time.time() - os.path.getmtime(CREDS_CACHE) if os.path.exists(CREDS_CACHE) else 9999
    suffixes  = list(range(suffix_start, suffix_start + total_users))

    if cache_age < 1800:
        print(f"\n[1/4] Cache hit ({cache_age:.0f}s old) — loading...", flush=True)
        with open(CREDS_CACHE) as f:
            users = json.load(f)
        # filter to requested range
        suffix_set = set(suffixes)
        users = [u for u in users if u["suffix"] in suffix_set]
        print(f"  {len(users)} from cache (need {total_users})", flush=True)
        if len(users) < total_users:
            # fetch missing
            cached_set = {u["suffix"] for u in users}
            missing    = [s for s in suffixes if s not in cached_set]
            print(f"  fetching {len(missing)} missing accounts...", flush=True)
            with ThreadPoolExecutor(max_workers=cred_workers) as pool:
                futs = {pool.submit(fetch_creds, s): s for s in missing}
                for fut in as_completed(futs):
                    r = fut.result()
                    if r: users.append(r)
    else:
        print(f"\n[1/4] Fetching {total_users} creds ({cred_workers} workers)...", flush=True)
        users = []
        done = errs = 0
        with ThreadPoolExecutor(max_workers=cred_workers) as pool:
            futs = {pool.submit(fetch_creds, s): s for s in suffixes}
            for fut in as_completed(futs):
                done += 1
                r = fut.result()
                if r: users.append(r)
                else: errs += 1
                if done % 50 == 0:
                    el = time.time() - t0 or 0.001
                    print(f"  {done}/{total_users} | ok:{len(users)} err:{errs} | {done/el:.1f}/s", flush=True)
        users.sort(key=lambda u: u["suffix"])
        with open(CREDS_CACHE, "w") as f:
            json.dump(users, f)
    print(f"  {len(users)}/{total_users} creds ready  ({time.time()-t0:.1f}s)", flush=True)
    if len(users) < 2:
        print("Too few users — aborting."); return

    # ── 2. STUN ────────────────────────────────────────────────────────────
    print("\n[2/4] STUN external IP...", flush=True)
    eip = stun_get_ip()
    lip = socket.gethostbyname(socket.gethostname())
    print(f"  ext={eip}  local={lip}", flush=True)

    # ── 3. register ────────────────────────────────────────────────────────
    print(f"\n[3/4] Registering {len(users)} users (concurrency={reg_sem_n})...", flush=True)
    reg_sem = asyncio.Semaphore(reg_sem_n)

    async def reg_i(i, u):
        ws, err = await register(u, reg_sem)
        return i, ws, err

    results = await asyncio.gather(*[reg_i(i, u) for i, u in enumerate(users)])
    ws_map  = {}
    reg_ok  = 0
    for idx, ws, err in results:
        if ws:
            ws_map[idx] = ws; reg_ok += 1
    print(f"  {reg_ok}/{len(users)} registered OK  ({time.time()-t0:.1f}s)", flush=True)
    if reg_ok < 2:
        print("Too few registrations — aborting."); return

    # ── 4. calls ────────────────────────────────────────────────────────────
    actual_pairs = sum(1 for i in range(0, len(users)-1, 2)
                       if i in ws_map and (i+1) in ws_map)
    print(f"\n[4/4] Launching {actual_pairs} call pairs (hold={hold}s, concurrency={call_sem_n})...", flush=True)
    stats    = {"connected": 0, "failed": 0, "completed": 0}
    call_sem = asyncio.Semaphore(call_sem_n)
    tasks    = []
    pid      = 0
    for i in range(0, len(users)-1, 2):
        if i in ws_map and (i+1) in ws_map:
            tasks.append(call_pair(pid, users[i], users[i+1],
                                   ws_map[i], ws_map[i+1],
                                   lip, eip, hold, call_sem, stats))
            pid += 1

    t_calls = time.time()
    await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = time.time() - t0
    print(flush=True)
    print("=" * 70, flush=True)
    print(f"  RESULTS", flush=True)
    print(f"  Pairs attempted  : {pid}", flush=True)
    print(f"  Connected        : {stats['connected']}", flush=True)
    print(f"  Failed           : {stats['failed']}", flush=True)
    print(f"  Completed (BYE)  : {stats['completed']}", flush=True)
    print(f"  Connect rate     : {stats['connected']/max(pid,1)*100:.1f}%", flush=True)
    print(f"  Call phase time  : {time.time()-t_calls:.1f}s", flush=True)
    print(f"  Total wall-clock : {elapsed:.1f}s", flush=True)
    print("=" * 70, flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--pairs",        type=int, default=50,  help="concurrent call pairs")
    p.add_argument("--hold",         type=int, default=60,  help="hold time per call (seconds)")
    p.add_argument("--suffix-start", type=int, default=2,   help="first account suffix (+N)")
    p.add_argument("--cred-workers", type=int, default=8,   help="thread workers for cred fetch")
    p.add_argument("--reg-sem",      type=int, default=30,  help="max concurrent SIP registrations")
    p.add_argument("--call-sem",     type=int, default=100, help="max concurrent call pairs")
    args = p.parse_args()

    asyncio.run(main(
        pairs        = args.pairs,
        hold         = args.hold,
        suffix_start = args.suffix_start,
        cred_workers = args.cred_workers,
        reg_sem_n    = args.reg_sem,
        call_sem_n   = args.call_sem,
    ))

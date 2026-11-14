import json, hashlib, base64, urllib.request, time, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from Crypto.Cipher import AES

API = "https://portal.qa.unifiedrtc.com/api/v1"
KEY_SECRET = "auditcryptosecret0123456789abcde"
TOTAL = 100
WORKERS = 10
OUT = "/tmp/creds_100.json"

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

def fetch(suffix):
    email = f"shreyanshdubey1122+{suffix}@gmail.com"
    for attempt in range(3):
        try:
            r = http_post(f"{API}/auth/login", {"email":email,"password":"Aadmin@123456!","device_type":"W"})
            tok = r["data"]["message"]["token"]
            r2 = http_get(f"{API}/sip/get-register-credentials", {"Authorization":f"Bearer {tok}"})
            d = decrypt_sip(r2["data"]["data"]["sipDetail"])
            return {"suffix":suffix,"ext":str(d["extension"]),"name":d["name"],"pw":d["password"]}
        except Exception:
            time.sleep(2 + attempt * 2)
    return None

_throttle = __import__('threading').Semaphore(1)

def fetch_throttled(suffix):
    time.sleep(0.3)  # max ~3 req/s across 5 workers
    return fetch(suffix)

print(f"Fetching creds for {TOTAL} users ({WORKERS} threads, throttled)...", flush=True)
t0 = time.time()
users = []
done = 0
errors = 0
suffixes = list(range(2, 2+TOTAL))

with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    futs = {pool.submit(fetch_throttled, s): s for s in suffixes}
    for fut in as_completed(futs):
        done += 1
        r = fut.result()
        if r: users.append(r)
        else: errors += 1
        if done % 200 == 0:
            elapsed = time.time()-t0
            rate = done/elapsed if elapsed > 0 else 1
            eta = (TOTAL-done)/rate
            print(f"  {done}/{TOTAL} | OK:{len(users)} err:{errors} | {rate:.1f}/s ETA:{eta:.0f}s", flush=True)

users.sort(key=lambda u: int(u["ext"]))
with open(OUT, "w") as f: json.dump(users, f)
print(f"DONE: {len(users)} creds saved → {OUT}  ({time.time()-t0:.0f}s)", flush=True)

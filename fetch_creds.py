"""
Fetch SIP credentials for N portal member accounts.
Rate limit on /auth/login: 5 req / 68s per IP.
Default: 1 request per 15s (safe). Override with --delay.

Usage:
  python3 fetch_creds.py                     # 200 accounts → /tmp/queue200_creds.json
  python3 fetch_creds.py --total 50 --out /tmp/my_creds.json
"""
import argparse, json, hashlib, base64, urllib.request, time
from Crypto.Cipher import AES

API = "https://portal.qa.unifiedrtc.com/api/v1"
KEY_SECRET = "auditcryptosecret0123456789abcde"

_ap = argparse.ArgumentParser()
_ap.add_argument('--total',  type=int,   default=200)
_ap.add_argument('--start',  type=int,   default=2,    help='email suffix start')
_ap.add_argument('--out',    default='/tmp/queue200_creds.json')
_ap.add_argument('--delay',  type=float, default=15.0, help='seconds between logins')
_args = _ap.parse_args()

TOTAL   = _args.total
WORKERS = 1
OUT     = _args.out

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
            # Try both known response shapes
            result = r2.get("data",{}).get("result",{}) or r2.get("data",{}).get("data",{})
            enc = result.get("sip_detail") or result.get("sipDetail","")
            if not enc:
                raise ValueError("no sip_detail in response")
            d = decrypt_sip(enc)
            return {
                "sip_user":  d.get("user") or d.get("name"),
                "extension": str(d.get("extension") or d.get("ext")),
                "domain":    d.get("domain","1792517466513.unifiedrtc.com"),
                "password":  d.get("password") or d.get("pw"),
                "email":     email,
                "token":     tok,
            }
        except Exception as e:
            time.sleep(2 + attempt * 2)
    return None

print(f"Fetching creds for {TOTAL} users (1 at a time, {_args.delay}s delay)...", flush=True)
t0 = time.time()
users = []
errors = 0
suffixes = list(range(_args.start, _args.start + TOTAL))

for i, s in enumerate(suffixes):
    r = fetch(s)
    if r:
        users.append(r)
    else:
        errors += 1
    done = i + 1
    if done % 10 == 0:
        elapsed = time.time() - t0
        eta = (TOTAL - done) * _args.delay
        print(f"  {done}/{TOTAL} | OK:{len(users)} err:{errors} | ETA:{eta:.0f}s", flush=True)
    if done < TOTAL:
        time.sleep(_args.delay)

users.sort(key=lambda u: int(u["extension"]))
with open(OUT, "w") as f:
    json.dump({"ts": time.time(), "creds": users}, f)
print(f"DONE: {len(users)} creds saved → {OUT}  ({time.time()-t0:.0f}s)", flush=True)

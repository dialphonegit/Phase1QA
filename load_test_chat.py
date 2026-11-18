#!/usr/bin/env python3
"""
Chat/Ticket Load Test for portal.qa.unifiedrtc.com
Modes:
  tickets   - create + close tickets in bulk (CONFIRMED working)
  login     - concurrent auth stress test
  mixed     - tickets + auth combined

Usage:
  python3 load_test_chat.py --mode tickets --workers 50 --duration 120
  python3 load_test_chat.py --mode login   --workers 20 --duration 60
  python3 load_test_chat.py --mode mixed   --workers 100 --duration 180
"""
import asyncio, aiohttp, argparse, json, uuid, time, random, signal, sys

BASE = 'https://portal.qa.unifiedrtc.com/api/v1'
ADMIN_EMAIL = 'shreyanshdubey1122@gmail.com'
ADMIN_PASS  = 'Aadmin@123456!'
PRIORITIES  = ['LOW', 'NORMAL', 'HIGH', 'URGENT']
STATUSES    = ['OPEN', 'IN_PROGRESS', 'CLOSED']

STOP = False

def handle_signal(sig, frame):
    global STOP
    STOP = True
    print('\n[STOP signal received — draining workers...]')

signal.signal(signal.SIGINT,  handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.created  = 0
        self.updated  = 0
        self.login_ok = 0
        self.login_fail = 0
        self.api_fail = 0
        self.start    = time.time()

    def report(self):
        elapsed = time.time() - self.start
        rate = (self.created + self.updated + self.login_ok) / max(1, elapsed) * 60
        print(
            f'[{elapsed:6.0f}s] tickets_created={self.created} '
            f'tickets_updated={self.updated} '
            f'logins_ok={self.login_ok} '
            f'login_fail={self.login_fail} '
            f'api_fail={self.api_fail} '
            f'ops/min={rate:.0f}'
        )

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
async def api_post(session, path, data, tok=None):
    headers = {'Content-Type': 'application/json', 'User-Agent': 'ChatLoadTest/1.0'}
    if tok:
        headers['Authorization'] = f'Bearer {tok}'
    try:
        async with session.post(BASE + path, json=data, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            body = await resp.json(content_type=None)
            return resp.status, body
    except Exception as e:
        return 0, {'error': str(e)}

async def login(session, email, password):
    code, body = await api_post(session, '/auth/login', {
        'email': email, 'password': password, 'device_type': 'W'
    })
    if code == 200:
        return body['data']['message']['token']
    return None

# ---------------------------------------------------------------------------
# Worker: ticket create+close loop
# ---------------------------------------------------------------------------
async def ticket_worker(worker_id, tok, stats, hold_secs):
    suffix = 2 + (worker_id % 400)
    while not STOP:
        subject = f'LT-{uuid.uuid4().hex[:8]}'
        priority = PRIORITIES[worker_id % len(PRIORITIES)]

        code, body = await api_post(None, '/ticket/create', {
            'subject': subject, 'description': f'load test worker {worker_id}',
            'priority': priority
        }, tok)
        # Note: session passed via global — handled in run_tickets

        if code == 200:
            stats.created += 1
            tid = body['data']['result']['_id']
            await asyncio.sleep(hold_secs)
            if STOP:
                break
            # close it
            code2, _ = await api_post(None, '/ticket/update', {
                'id': tid, 'status': 'CLOSED'
            }, tok)
            if code2 == 200:
                stats.updated += 1
            else:
                stats.api_fail += 1
        elif code == 429:
            await asyncio.sleep(2 + random.uniform(0, 2))
        else:
            stats.api_fail += 1
            await asyncio.sleep(1)

# Real workers that hold the aiohttp session
async def ticket_worker_v2(worker_id, session, tok_pool, stats, hold_secs):
    tok = random.choice(tok_pool)
    while not STOP:
        subject = f'LT-{uuid.uuid4().hex[:8]}'
        priority = PRIORITIES[worker_id % len(PRIORITIES)]

        code, body = await api_post(session, '/ticket/create', {
            'subject': subject, 'description': f'load test w{worker_id}',
            'priority': priority
        }, tok)

        if code == 200:
            stats.created += 1
            tid = body['data']['result']['_id']
            await asyncio.sleep(hold_secs)
            if STOP:
                break
            code2, _ = await api_post(session, '/ticket/update', {
                'id': tid, 'status': 'CLOSED'
            }, tok)
            if code2 == 200:
                stats.updated += 1
            else:
                stats.api_fail += 1
        elif code == 429:
            await asyncio.sleep(3 + random.uniform(0, 3))
        else:
            stats.api_fail += 1
            await asyncio.sleep(0.5)

# ---------------------------------------------------------------------------
# Worker: login stress
# ---------------------------------------------------------------------------
async def login_worker(worker_id, session, stats, suffix_start):
    suffix = suffix_start + (worker_id % 400)
    email = f'shreyanshdubey1122+{suffix}@gmail.com'
    while not STOP:
        tok = await login(session, email, ADMIN_PASS)
        if tok:
            stats.login_ok += 1
        else:
            stats.login_fail += 1
            await asyncio.sleep(2)
        await asyncio.sleep(random.uniform(0.5, 1.5))

# ---------------------------------------------------------------------------
# Token pool: login with N accounts up front
# ---------------------------------------------------------------------------
async def build_token_pool(session, n_tokens, suffix_start, stats):
    sem = asyncio.Semaphore(5)
    tokens = []

    async def fetch_one(i):
        async with sem:
            suffix = suffix_start + i
            email = f'shreyanshdubey1122+{suffix}@gmail.com'
            tok = await login(session, email, ADMIN_PASS)
            if tok:
                tokens.append(tok)
                stats.login_ok += 1
            await asyncio.sleep(0.3)

    await asyncio.gather(*[fetch_one(i) for i in range(n_tokens)])
    return tokens

# ---------------------------------------------------------------------------
# Stats printer
# ---------------------------------------------------------------------------
async def stats_loop(stats, interval=15):
    while not STOP:
        await asyncio.sleep(interval)
        stats.report()

# ---------------------------------------------------------------------------
# Main runners
# ---------------------------------------------------------------------------
async def run_tickets(args, stats):
    connector = aiohttp.TCPConnector(limit=200, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        print(f'[+] Building token pool with {min(args.workers, 50)} tokens...')
        tok_pool = await build_token_pool(session, min(args.workers, 50), args.suffix_start, stats)
        if not tok_pool:
            print('[!] No tokens — aborting')
            return
        print(f'[+] Got {len(tok_pool)} tokens. Launching {args.workers} ticket workers...')

        workers = [
            ticket_worker_v2(i, session, tok_pool, stats, args.hold)
            for i in range(args.workers)
        ]
        printer = stats_loop(stats)
        done_flag = asyncio.create_task(asyncio.sleep(args.duration))

        await asyncio.gather(
            asyncio.gather(*[asyncio.create_task(w) for w in workers],
                           return_exceptions=True),
            asyncio.create_task(printer),
            done_flag,
            return_exceptions=True
        )

async def run_login(args, stats):
    connector = aiohttp.TCPConnector(limit=200, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        print(f'[+] Launching {args.workers} login workers...')
        workers = [
            login_worker(i, session, stats, args.suffix_start)
            for i in range(args.workers)
        ]
        printer = stats_loop(stats)
        # run for duration then stop
        async def stop_after():
            await asyncio.sleep(args.duration)
            global STOP
            STOP = True

        await asyncio.gather(
            asyncio.gather(*[asyncio.create_task(w) for w in workers],
                           return_exceptions=True),
            asyncio.create_task(printer),
            asyncio.create_task(stop_after()),
            return_exceptions=True
        )

async def run_mixed(args, stats):
    """Half workers do tickets, half do login stress."""
    connector = aiohttp.TCPConnector(limit=300, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        half = args.workers // 2
        print(f'[+] Building token pool...')
        tok_pool = await build_token_pool(session, min(half, 30), args.suffix_start, stats)
        if not tok_pool:
            print('[!] No tokens — aborting')
            return
        print(f'[+] {len(tok_pool)} tokens. {half} ticket workers + {args.workers - half} login workers')

        ticket_workers = [
            ticket_worker_v2(i, session, tok_pool, stats, args.hold)
            for i in range(half)
        ]
        login_workers = [
            login_worker(half + i, session, stats, args.suffix_start + 100)
            for i in range(args.workers - half)
        ]
        printer = stats_loop(stats)

        async def stop_after():
            await asyncio.sleep(args.duration)
            global STOP
            STOP = True

        all_workers = ticket_workers + login_workers
        await asyncio.gather(
            asyncio.gather(*[asyncio.create_task(w) for w in all_workers],
                           return_exceptions=True),
            asyncio.create_task(printer),
            asyncio.create_task(stop_after()),
            return_exceptions=True
        )

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description='Chat/Ticket load test')
    p.add_argument('--mode',         default='tickets', choices=['tickets','login','mixed'])
    p.add_argument('--workers',      type=int, default=50,  help='concurrent worker count')
    p.add_argument('--duration',     type=int, default=120, help='test duration in seconds')
    p.add_argument('--hold',         type=float, default=5, help='seconds to hold ticket open before closing')
    p.add_argument('--suffix-start', type=int, default=2,   help='account suffix start (shreyanshdubey1122+N)')
    args = p.parse_args()

    print(f'=== Chat Load Test === mode={args.mode} workers={args.workers} duration={args.duration}s hold={args.hold}s')
    stats = Stats()

    if args.mode == 'tickets':
        asyncio.run(run_tickets(args, stats))
    elif args.mode == 'login':
        asyncio.run(run_login(args, stats))
    elif args.mode == 'mixed':
        asyncio.run(run_mixed(args, stats))

    global STOP
    STOP = True
    print('\n=== FINAL STATS ===')
    stats.report()

if __name__ == '__main__':
    main()

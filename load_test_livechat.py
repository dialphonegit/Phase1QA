#!/usr/bin/env python3
"""
Live Chat (Socket.io) Load Test for portal.qa.unifiedrtc.com
Tests the real-time chat WebSocket layer with concurrent connections.

Usage:
  python3 load_test_livechat.py --connections 200 --duration 180
  python3 load_test_livechat.py --connections 50  --duration 60 --dry-run
"""
import asyncio, aiohttp, argparse, json, uuid, time, random, signal, re
import urllib.request

BASE     = 'https://portal.qa.unifiedrtc.com'
API_BASE = BASE + '/api/v1'
SIO_URL  = BASE + '/socket.io/?EIO=4&transport=websocket'
SIO_POLL = BASE + '/socket.io/?EIO=4&transport=polling'

ADMIN_EMAIL = 'shreyanshdubey1122@gmail.com'
ADMIN_PASS  = 'Aadmin@123456!'
TOKEN_CACHE = '/tmp/livechat_tokens.json'
TOKEN_TTL   = 3000

STOP = False

def handle_signal(sig, frame):
    global STOP
    STOP = True
    print('\n[STOP — draining...]')

signal.signal(signal.SIGINT,  handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.ws_connected = 0
        self.ws_failed    = 0
        self.msgs_sent    = 0
        self.msgs_recv    = 0
        self.auth_ok      = 0
        self.auth_fail    = 0
        self.errors       = 0
        self.start        = time.time()

    def report(self):
        t = time.time() - self.start
        rate = self.msgs_sent / max(1, t) * 60
        print(
            f'[{t:5.0f}s] ws_conn={self.ws_connected} ws_fail={self.ws_failed} '
            f'msgs_sent={self.msgs_sent} msgs_recv={self.msgs_recv} '
            f'auth_ok={self.auth_ok} errors={self.errors} '
            f'msg/min={rate:.0f}'
        )

# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------
def load_cached_tokens():
    try:
        d = json.load(open(TOKEN_CACHE))
        if time.time() - d['ts'] < TOKEN_TTL:
            return d.get('tokens', []), d.get('uids', [])
    except Exception:
        pass
    return [], []

def save_tokens(tokens, uids):
    with open(TOKEN_CACHE, 'w') as f:
        json.dump({'ts': time.time(), 'tokens': tokens, 'uids': uids}, f)

def sync_login(email, password):
    req = urllib.request.Request(
        API_BASE + '/auth/login',
        json.dumps({'email': email, 'password': password, 'device_type': 'W'}).encode(),
        headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
            return d['data']['message']['token'], d['data']['message']['_id']
    except urllib.request.HTTPError as e:
        body = json.loads(e.read())
        retry = e.headers.get('Retry-After', '?')
        print(f'  Login {email}: {e.code} retry={retry}s — {body.get("error",{}).get("message","")}')
        return None, None

async def build_token_pool(n, suffix_start, stats):
    tokens, uids = load_cached_tokens()
    if len(tokens) >= n:
        print(f'[+] {n} cached tokens loaded (skipping login)')
        stats.auth_ok += n
        return tokens[:n], uids[:n]

    print(f'[+] Cache has {len(tokens)}/{n} — fetching {n - len(tokens)} more (rate limit: 2/s)...')
    sem = asyncio.Semaphore(2)
    start_i = len(tokens)

    async def fetch(i):
        async with sem:
            suffix = suffix_start + i
            email = f'shreyanshdubey1122+{suffix}@gmail.com'
            loop = asyncio.get_event_loop()
            tok, uid = await loop.run_in_executor(None, sync_login, email, ADMIN_PASS)
            if tok:
                tokens.append(tok)
                uids.append(uid)
                stats.auth_ok += 1
            else:
                stats.auth_fail += 1
            await asyncio.sleep(1.5)

    await asyncio.gather(*[fetch(start_i + i) for i in range(n - start_i)])
    if tokens:
        save_tokens(tokens, uids)
    return tokens, uids

# ---------------------------------------------------------------------------
# Socket.io raw WebSocket worker
# EIO4 wire format:
#   2probe / 3probe  — ping/pong upgrade
#   5                — upgrade complete
#   40               — connect to namespace /
#   40/chat,         — connect to namespace /chat
#   42["event",data] — message emit
#   43N[data]        — ack reply
#   2 / 3            — EIO ping/pong heartbeat
# ---------------------------------------------------------------------------
import websockets

async def sio_worker(worker_id, token, uid, stats):
    """One persistent Socket.io connection: authenticate, join chat, send messages in a loop."""
    import websockets.exceptions

    # Step 1: polling handshake to get sid
    try:
        req = urllib.request.Request(SIO_POLL, headers={'User-Agent':'Mozilla/5.0','Origin':BASE})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode()
        sid = json.loads(raw[1:])['sid']
    except Exception as e:
        stats.ws_failed += 1
        return

    ws_url = f'{BASE.replace("https://","wss://")}/socket.io/?EIO=4&transport=websocket&sid={sid}'
    headers = {'Origin': BASE, 'User-Agent': 'Mozilla/5.0'}

    try:
        async with websockets.connect(ws_url, additional_headers=headers,
                                       ping_interval=None, close_timeout=5) as ws:
            # EIO upgrade handshake
            await ws.send('2probe')
            resp = await asyncio.wait_for(ws.recv(), timeout=5)
            if resp != '3probe':
                stats.ws_failed += 1
                return
            await ws.send('5')  # upgrade confirmed

            # Connect to default namespace with auth token
            connect_payload = json.dumps({'token': token})
            await ws.send(f'40{connect_payload}')

            # Wait for connect ack or error
            try:
                ack = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                stats.ws_failed += 1
                return

            if ack.startswith('44'):
                # Auth error
                err = json.loads(ack[2:])
                stats.ws_failed += 1
                if worker_id < 3:
                    print(f'  [w{worker_id}] auth error: {err}')
                return

            stats.ws_connected += 1
            if worker_id < 3:
                print(f'  [w{worker_id}] connected! ack={repr(ack[:100])}')

            # Emit join/subscribe events for chat
            msg_id = 0
            chat_id = str(uuid.uuid4())

            async def send_msg(event, data):
                nonlocal msg_id
                payload = f'42{json.dumps([event, data])}'
                await ws.send(payload)
                stats.msgs_sent += 1
                msg_id += 1

            # Try common chat events
            await send_msg('join_room', {'room': 'general', 'token': token})
            await send_msg('subscribe', {'channel': 'chat', 'token': token})
            await send_msg('authenticate', {'token': token})

            # Main message loop
            ping_time = time.time()
            loop_count = 0

            async def recv_loop():
                while not STOP:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2)
                        if msg == '2':  # EIO ping
                            await ws.send('3')  # pong
                        elif msg.startswith('42'):
                            stats.msgs_recv += 1
                            if worker_id < 2:
                                print(f'  [w{worker_id}] recv: {repr(msg[:200])}')
                    except asyncio.TimeoutError:
                        pass
                    except Exception:
                        break

            async def send_loop():
                nonlocal loop_count
                while not STOP:
                    # Send a chat message every 2-5 seconds
                    await asyncio.sleep(random.uniform(2, 5))
                    if STOP:
                        break
                    try:
                        await send_msg('message', {
                            'room': 'general',
                            'text': f'load-test-{worker_id}-{loop_count}',
                            'chatId': chat_id,
                            'token': token,
                        })
                        await send_msg('send_message', {
                            'message': f'lt-{worker_id}-{loop_count}',
                            'channelId': chat_id,
                        })
                        loop_count += 1
                        # EIO heartbeat ping every 20s
                        if time.time() - ping_time > 20:
                            await ws.send('2')
                    except Exception:
                        break

            await asyncio.gather(recv_loop(), send_loop(), return_exceptions=True)

    except websockets.exceptions.ConnectionClosedError as e:
        stats.errors += 1
    except Exception as e:
        stats.ws_failed += 1
        if worker_id < 3:
            print(f'  [w{worker_id}] error: {type(e).__name__}: {e}')

# ---------------------------------------------------------------------------
# Stats printer
# ---------------------------------------------------------------------------
async def stats_loop(stats, interval=15):
    while not STOP:
        await asyncio.sleep(interval)
        stats.report()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run(args, stats):
    print(f'[+] Building token pool ({args.connections} tokens)...')
    tokens, uids = await build_token_pool(args.connections, args.suffix_start, stats)

    if not tokens:
        print('[!] No tokens available — aborting. Check rate limit.')
        return

    actual = min(len(tokens), args.connections)
    print(f'[+] Launching {actual} WebSocket connections...')

    workers = [
        sio_worker(i, tokens[i % len(tokens)], uids[i % len(uids)] if uids else None, stats)
        for i in range(actual)
    ]

    async def stop_after():
        await asyncio.sleep(args.duration)
        global STOP
        STOP = True
        print(f'\n[+] Duration {args.duration}s reached — stopping')

    await asyncio.gather(
        asyncio.gather(*[asyncio.create_task(w) for w in workers], return_exceptions=True),
        asyncio.create_task(stats_loop(stats)),
        asyncio.create_task(stop_after()),
        return_exceptions=True
    )

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--connections',  type=int, default=50,   help='concurrent WS connections')
    p.add_argument('--duration',     type=int, default=120,  help='test duration seconds')
    p.add_argument('--suffix-start', type=int, default=2,    help='account suffix start')
    p.add_argument('--dry-run',      action='store_true',    help='only fetch tokens, skip WS test')
    args = p.parse_args()

    print(f'=== Live Chat Load Test === connections={args.connections} duration={args.duration}s')
    stats = Stats()

    if args.dry_run:
        async def dry():
            tokens, uids = await build_token_pool(args.connections, args.suffix_start, stats)
            print(f'[dry-run] Got {len(tokens)} tokens')
        asyncio.run(dry())
    else:
        asyncio.run(run(args, stats))

    global STOP
    STOP = True
    print('\n=== FINAL ===')
    stats.report()

if __name__ == '__main__':
    main()

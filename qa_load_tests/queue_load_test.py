#!/usr/bin/env python3
"""
Queue Load Test — SIP over WebSocket
Two-sided test:
  - N agents register to portal.qa.unifiedrtc.com and ANSWER incoming calls (linear pickup)
  - M callers dial queue extension 0200 (or any ext via --queue-ext)
  - Calls are properly connected: 180 Ringing → 200 OK → ACK → hold → BYE

Usage:
  python3 queue_load_test.py --agents 200 --callers 100 --hold 60
  python3 queue_load_test.py --agents 10  --callers 5  --hold 10 --dry-run
  python3 queue_load_test.py --mode callers-only --callers 50 --queue-ext 0200

Credentials: /tmp/queue200_creds.json (or --creds-file)
  Fetch with:  python3 fetch_creds.py
"""
import asyncio, argparse, hashlib, json, re, time, uuid
import websockets
import websockets.exceptions

SIP_WS  = 'wss://app.qa.unifiedrtc.com/ws/'
SIP_DOM = '1792517466513.unifiedrtc.com'
ORIGIN  = 'https://portal.qa.unifiedrtc.com'


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.agents_reg    = 0
        self.agents_fail   = 0
        self.calls_placed  = 0
        self.calls_answered = 0
        self.calls_fail    = 0
        self.start = time.time()

    def report(self):
        t = time.time() - self.start
        print(
            f'[{t:5.0f}s] agents={self.agents_reg} '
            f'placed={self.calls_placed} answered={self.calls_answered} '
            f'agent_fail={self.agents_fail} call_fail={self.calls_fail}',
            flush=True
        )


# ---------------------------------------------------------------------------
# SIP helpers
# ---------------------------------------------------------------------------
def digest(user, pw, realm, nonce, uri, method):
    ha1 = hashlib.md5(f'{user}:{realm}:{pw}'.encode()).hexdigest()
    ha2 = hashlib.md5(f'{method}:{uri}'.encode()).hexdigest()
    return hashlib.md5(f'{ha1}:{nonce}:{ha2}'.encode()).hexdigest()

def via(domain):
    return f'SIP/2.0/WSS {domain};branch=z9hG4bK{uuid.uuid4().hex[:8]}'

def register_msg(ext, domain, cid, cseq, expires=300, auth=None):
    hdrs = (
        f'REGISTER sip:{domain} SIP/2.0\r\n'
        f'Via: {via(domain)}\r\n'
        f'From: <sip:{ext}@{domain}>;tag={uuid.uuid4().hex[:8]}\r\n'
        f'To: <sip:{ext}@{domain}>\r\n'
        f'Call-ID: {cid}\r\nCSeq: {cseq} REGISTER\r\n'
        f'Contact: <sip:{ext}@wss.{domain};transport=ws>;expires={expires}\r\n'
        f'Max-Forwards: 70\r\n'
    )
    if auth:
        hdrs += f'Authorization: {auth}\r\n'
    return hdrs + 'Content-Length: 0\r\n\r\n'

def _make_ws():
    return websockets.connect(
        SIP_WS, subprotocols=['sip'],
        additional_headers={'Origin': ORIGIN},
        ping_interval=25, close_timeout=5
    )

async def sip_register(ws, sip_user, pw, ext, domain):
    """Full challenge-response REGISTER. Returns True on success."""
    cid = uuid.uuid4().hex
    await ws.send(register_msg(ext, domain, cid, 1))
    r1 = await asyncio.wait_for(ws.recv(), timeout=8)
    if '401' not in r1:
        return False
    realm = re.search(r'realm="([^"]+)"', r1).group(1)
    nonce = re.search(r'nonce="([^"]+)"', r1).group(1)
    rh = digest(sip_user, pw, realm, nonce, f'sip:{domain}', 'REGISTER')
    auth = (f'Digest username="{sip_user}",realm="{realm}",nonce="{nonce}",'
            f'uri="sip:{domain}",response="{rh}"')
    await ws.send(register_msg(ext, domain, cid, 2, auth=auth))
    r2 = await asyncio.wait_for(ws.recv(), timeout=8)
    return '200 OK' in r2


# ---------------------------------------------------------------------------
# Agent: register + answer incoming INVITEs (LINEAR pickup — first available)
# ---------------------------------------------------------------------------
async def agent_worker(cred, slot_id, hold_secs, stats, sem):
    sip_user = cred['sip_user']
    ext      = cred['extension']
    domain   = cred.get('domain', SIP_DOM)
    pw       = cred['password']

    await asyncio.sleep(slot_id * 0.08)   # stagger 80 ms per slot

    try:
        ws = await _make_ws()
        async with sem:
            ok = await sip_register(ws, sip_user, pw, ext, domain)
        if not ok:
            stats.agents_fail += 1
            await ws.close(); return
        stats.agents_reg += 1

        # Stay alive and answer incoming calls
        deadline = time.time() + 300   # 5-minute session
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

            if not msg.startswith('INVITE '):
                continue

            # Parse headers
            def hdr(name):
                m = re.search(rf'{name}:\s*(.+)', msg, re.I)
                return m.group(1).strip() if m else ''

            inc_cid  = hdr('Call-ID')
            from_hdr = hdr('From')
            to_hdr   = hdr('To')
            via_hdr  = hdr('Via')
            cseq_num = (re.search(r'CSeq:\s*(\d+)', msg, re.I) or re.match(r'', '')).group(1) if re.search(r'CSeq:\s*(\d+)', msg, re.I) else '1'
            to_tag   = uuid.uuid4().hex[:8]

            ans_sdp = (
                'v=0\r\no=- 0 1 IN IP4 0.0.0.0\r\ns=-\r\n'
                'c=IN IP4 0.0.0.0\r\nt=0 0\r\n'
                'm=audio 20000 RTP/SAVPF 111\r\n'
                'a=rtpmap:111 opus/48000/2\r\na=sendrecv\r\n'
            )

            # 180 Ringing
            ring = (
                f'SIP/2.0 180 Ringing\r\nVia: {via_hdr}\r\n'
                f'From: {from_hdr}\r\nTo: {to_hdr};tag={to_tag}\r\n'
                f'Call-ID: {inc_cid}\r\nCSeq: {cseq_num} INVITE\r\n'
                f'Content-Length: 0\r\n\r\n'
            )
            await ws.send(ring)
            await asyncio.sleep(0.3)

            # 200 OK — pick up
            ok_resp = (
                f'SIP/2.0 200 OK\r\nVia: {via_hdr}\r\n'
                f'From: {from_hdr}\r\nTo: {to_hdr};tag={to_tag}\r\n'
                f'Call-ID: {inc_cid}\r\nCSeq: {cseq_num} INVITE\r\n'
                f'Contact: <sip:{ext}@wss.{domain};transport=ws>\r\n'
                f'Content-Type: application/sdp\r\n'
                f'Content-Length: {len(ans_sdp)}\r\n\r\n{ans_sdp}'
            )
            await ws.send(ok_resp)
            stats.calls_answered += 1

            # Hold call then hang up
            await asyncio.sleep(hold_secs)
            bye = (
                f'BYE sip:{domain} SIP/2.0\r\n'
                f'Via: {via(domain)}\r\n'
                f'From: {from_hdr}\r\nTo: {to_hdr};tag={to_tag}\r\n'
                f'Call-ID: {inc_cid}\r\nCSeq: 2 BYE\r\n'
                f'Max-Forwards: 70\r\nContent-Length: 0\r\n\r\n'
            )
            try:
                await ws.send(bye)
            except Exception:
                pass
            break

        await ws.close()
    except Exception:
        stats.agents_fail += 1


# ---------------------------------------------------------------------------
# Caller: register + INVITE queue extension
# ---------------------------------------------------------------------------
async def caller_worker(cred, queue_ext, hold_secs, stats, sem):
    sip_user = cred['sip_user']
    ext      = cred['extension']
    domain   = cred.get('domain', SIP_DOM)
    pw       = cred['password']
    cid      = uuid.uuid4().hex
    from_tag = uuid.uuid4().hex[:8]

    sdp = (
        'v=0\r\no=- 0 1 IN IP4 0.0.0.0\r\ns=-\r\n'
        'c=IN IP4 0.0.0.0\r\nt=0 0\r\n'
        'm=audio 20000 RTP/SAVPF 111\r\n'
        'a=rtpmap:111 opus/48000/2\r\na=sendrecv\r\n'
    )

    try:
        ws = await _make_ws()
        async with sem:
            ok = await sip_register(ws, sip_user, pw, ext, domain)
        if not ok:
            stats.calls_fail += 1
            await ws.close(); return

        # INVITE queue
        invite_uri = f'sip:{queue_ext}@{domain}'
        invite = (
            f'INVITE {invite_uri} SIP/2.0\r\n'
            f'Via: {via(domain)}\r\n'
            f'From: <sip:{ext}@{domain}>;tag={from_tag}\r\n'
            f'To: <sip:{queue_ext}@{domain}>\r\n'
            f'Call-ID: {cid}\r\nCSeq: 1 INVITE\r\nMax-Forwards: 70\r\n'
            f'Contact: <sip:{ext}@wss.{domain};transport=ws>\r\n'
            f'Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}'
        )
        await ws.send(invite)
        stats.calls_placed += 1

        for _ in range(30):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=6)
            except asyncio.TimeoutError:
                break

            if '407 ' in msg:
                realm = re.search(r'realm="([^"]+)"', msg).group(1)
                nonce = re.search(r'nonce="([^"]+)"', msg).group(1)
                rh = digest(sip_user, pw, realm, nonce, invite_uri, 'INVITE')
                proxy_auth = (
                    f'Digest username="{sip_user}",realm="{realm}",nonce="{nonce}",'
                    f'uri="{invite_uri}",response="{rh}"'
                )
                invite2 = (
                    f'INVITE {invite_uri} SIP/2.0\r\n'
                    f'Via: {via(domain)}\r\n'
                    f'From: <sip:{ext}@{domain}>;tag={from_tag}\r\n'
                    f'To: <sip:{queue_ext}@{domain}>\r\n'
                    f'Call-ID: {cid}\r\nCSeq: 2 INVITE\r\nMax-Forwards: 70\r\n'
                    f'Contact: <sip:{ext}@wss.{domain};transport=ws>\r\n'
                    f'Proxy-Authorization: {proxy_auth}\r\n'
                    f'Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}'
                )
                await ws.send(invite2)

            elif '200 OK' in msg and 'CSeq' in msg:
                # Send ACK
                to_tag = ''
                tm = re.search(r'To:.*?tag=(\S+)', msg, re.I)
                if tm:
                    to_tag = f';tag={tm.group(1)}'
                ack = (
                    f'ACK {invite_uri} SIP/2.0\r\n'
                    f'Via: {via(domain)}\r\n'
                    f'From: <sip:{ext}@{domain}>;tag={from_tag}\r\n'
                    f'To: <sip:{queue_ext}@{domain}>{to_tag}\r\n'
                    f'Call-ID: {cid}\r\nCSeq: 2 ACK\r\n'
                    f'Max-Forwards: 70\r\nContent-Length: 0\r\n\r\n'
                )
                await ws.send(ack)
                await asyncio.sleep(hold_secs)
                bye = (
                    f'BYE {invite_uri} SIP/2.0\r\n'
                    f'Via: {via(domain)}\r\n'
                    f'From: <sip:{ext}@{domain}>;tag={from_tag}\r\n'
                    f'To: <sip:{queue_ext}@{domain}>{to_tag}\r\n'
                    f'Call-ID: {cid}\r\nCSeq: 3 BYE\r\n'
                    f'Max-Forwards: 70\r\nContent-Length: 0\r\n\r\n'
                )
                await ws.send(bye)
                await ws.close()
                return

        stats.calls_fail += 1
        await ws.close()
    except Exception:
        stats.calls_fail += 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run(args):
    creds_file = args.creds_file
    try:
        raw = json.load(open(creds_file))
        creds = raw if isinstance(raw, list) else raw.get('creds', [])
    except FileNotFoundError:
        print(f'[!] Creds file not found: {creds_file}')
        print('    Run:  python3 fetch_creds.py  to generate it')
        return

    # Remap legacy format {ext, pw, name} → {sip_user, extension, password, domain}
    for c in creds:
        if 'pw' in c and 'password' not in c:
            c['password'] = c.pop('pw')
        if 'ext' in c and 'extension' not in c:
            c['extension'] = c.pop('ext')
        if 'name' in c and 'sip_user' not in c:
            c['sip_user'] = c.pop('name')
        c.setdefault('domain', SIP_DOM)

    if not creds:
        print('[!] No credentials loaded. Check --creds-file.')
        return

    n_agents  = min(args.agents,  len(creds))
    n_callers = min(args.callers, len(creds))
    agent_creds  = creds[:n_agents]
    caller_creds = creds[n_agents:n_agents+n_callers] or creds[:n_callers]

    print(f'Queue: {args.queue_ext}@{SIP_DOM}')
    print(f'Agents: {n_agents}  Callers: {n_callers}  Hold: {args.hold}s')
    if args.dry_run:
        print('[dry-run] Exiting before WS connections.')
        return

    stats = Stats()
    sem   = asyncio.Semaphore(25)   # max concurrent SIP handshakes

    # Phase 1: register agents
    print(f'\n[1] Registering {n_agents} agents...')
    agent_tasks = [
        asyncio.create_task(agent_worker(c, i, args.hold, stats, sem))
        for i, c in enumerate(agent_creds)
    ]
    await asyncio.sleep(12)
    stats.report()

    # Phase 2: launch callers
    print(f'\n[2] {n_callers} callers → queue {args.queue_ext}...')
    call_tasks = [
        asyncio.create_task(caller_worker(c, args.queue_ext, args.hold, stats, sem))
        for c in caller_creds
    ]

    async def printer():
        for _ in range(20):
            await asyncio.sleep(15)
            stats.report()

    await asyncio.gather(
        *agent_tasks, *call_tasks,
        asyncio.create_task(printer()),
        return_exceptions=True
    )

    print('\n=== FINAL ===')
    stats.report()


def main():
    p = argparse.ArgumentParser(description='SIP Queue Load Test')
    p.add_argument('--agents',     type=int,   default=200,                   help='SIP agents to register (answer side)')
    p.add_argument('--callers',    type=int,   default=100,                   help='SIP callers to place into queue')
    p.add_argument('--queue-ext',  default='0200',                            help='Queue extension to dial')
    p.add_argument('--hold',       type=float, default=60,                    help='Seconds to hold each call after answer')
    p.add_argument('--creds-file', default='/tmp/queue100_creds.json',        help='JSON file with SIP credentials')
    p.add_argument('--mode',       choices=['full','agents-only','callers-only'], default='full')
    p.add_argument('--dry-run',    action='store_true')
    args = p.parse_args()

    if args.mode == 'agents-only':
        args.callers = 0
    elif args.mode == 'callers-only':
        args.agents = 0

    asyncio.run(run(args))


if __name__ == '__main__':
    main()

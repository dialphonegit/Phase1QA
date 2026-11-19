"""Async SIP caller engine — fires calls into the queue while Playwright agents answer."""
import asyncio
import hashlib
import logging
import os
import re
import ssl
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Awaitable

import websockets

log = logging.getLogger(__name__)

SIP_DOMAIN = os.getenv("SIP_DOMAIN", "1792517466513.unifiedrtc.com")
SIP_WS_URL = os.getenv("SIP_WS_URL", "wss://app.qa.unifiedrtc.com/ws/")


@dataclass
class CallerStats:
    placed:   int = 0
    answered: int = 0
    failed:   int = 0
    active:   int = 0
    start_ts: float = field(default_factory=time.time)

    def snapshot(self):
        return {"placed": self.placed, "answered": self.answered,
                "failed": self.failed, "active": self.active,
                "elapsed": round(time.time() - self.start_ts)}


class SIPCaller:
    def __init__(self, cred: dict, queue_ext: str, hold: float,
                 stats: CallerStats, event_cb=None):
        self.cred      = cred
        self.queue_ext = queue_ext
        self.hold      = hold
        self.stats     = stats
        self.event_cb  = event_cb
        self._tag      = uuid.uuid4().hex[:8]
        self._call_id  = uuid.uuid4().hex

    async def run(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            async with websockets.connect(
                SIP_WS_URL, subprotocols=["sip"],
                ssl=ctx, ping_interval=None, open_timeout=15
            ) as ws:
                await self._register(ws)
                await self._invite_loop(ws)
        except Exception as e:
            self.stats.failed += 1
            log.debug("Caller %s: %s", self.cred.get("extension"), e)

    async def _register(self, ws):
        u, d, pw = self.cred["sip_user"], SIP_DOMAIN, self.cred["password"]
        t = self._tag
        reg = (f"REGISTER sip:{d} SIP/2.0\r\nVia: SIP/2.0/WSS {d};branch=z9hG4bK{t}\r\n"
               f"From: <sip:{u}@{d}>;tag={t}\r\nTo: <sip:{u}@{d}>\r\n"
               f"Call-ID: reg-{self._call_id}\r\nCSeq: 1 REGISTER\r\n"
               f"Contact: <sip:{u}@{d};transport=ws>\r\nExpires: 300\r\nContent-Length: 0\r\n\r\n")
        await ws.send(reg)
        resp = await asyncio.wait_for(ws.recv(), 10)
        if "401" in resp or "407" in resp:
            realm = re.search(r'realm="([^"]+)"', resp)
            nonce = re.search(r'nonce="([^"]+)"', resp)
            if realm and nonce:
                auth = _digest(u, pw, realm.group(1), nonce.group(1), "REGISTER", f"sip:{d}")
                reg2 = reg.replace("CSeq: 1 REGISTER", "CSeq: 2 REGISTER") \
                           .replace("Content-Length: 0", f"Authorization: {auth}\r\nContent-Length: 0")
                await ws.send(reg2)
                await asyncio.wait_for(ws.recv(), 10)

    async def _invite_loop(self, ws):
        u, d, pw = self.cred["sip_user"], SIP_DOMAIN, self.cred["password"]
        q, t = self.queue_ext, self._tag
        sdp = (_sdp(u))
        invite = (f"INVITE sip:{q}@{d} SIP/2.0\r\nVia: SIP/2.0/WSS {d};branch=z9hG4bK{t}i\r\n"
                  f"From: <sip:{u}@{d}>;tag={t}\r\nTo: <sip:{q}@{d}>\r\n"
                  f"Call-ID: {self._call_id}\r\nCSeq: 1 INVITE\r\n"
                  f"Contact: <sip:{u}@{d};transport=ws>\r\n"
                  f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}")
        await ws.send(invite)
        self.stats.placed += 1
        self.stats.active += 1
        if self.event_cb: await self.event_cb("placed", {"ext": u})

        ring_at = time.time()
        answered = False
        seq = 2
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), 90)
            except asyncio.TimeoutError:
                break
            if "407" in msg or "401" in msg:
                r = re.search(r'realm="([^"]+)"', msg)
                n = re.search(r'nonce="([^"]+)"', msg)
                if r and n:
                    auth = _digest(u, pw, r.group(1), n.group(1), "INVITE", f"sip:{q}@{d}")
                    await ws.send(_ack(self._call_id, u, q, d, t, 1))
                    inv2 = invite.replace("CSeq: 1 INVITE", f"CSeq: {seq} INVITE") \
                                 .replace("Content-Type:", f"Authorization: {auth}\r\nContent-Type:")
                    seq += 1
                    await ws.send(inv2)
            elif "180" in msg or "183" in msg:
                pass
            elif "200" in msg and "INVITE" not in msg.split("\r\n")[0]:
                answered = True
                self.stats.answered += 1
                await ws.send(_ack(self._call_id, u, q, d, t, seq))
                if self.event_cb: await self.event_cb("answered", {"ext": u, "wait": round(time.time()-ring_at,1)})
                await asyncio.sleep(self.hold)
                bye = (f"BYE sip:{q}@{d} SIP/2.0\r\nVia: SIP/2.0/WSS {d};branch=z9hG4bK{t}b\r\n"
                       f"From: <sip:{u}@{d}>;tag={t}\r\nTo: <sip:{q}@{d}>\r\n"
                       f"Call-ID: {self._call_id}\r\nCSeq: {seq+1} BYE\r\nContent-Length: 0\r\n\r\n")
                await ws.send(bye)
                if self.event_cb: await self.event_cb("ended", {"ext": u})
                break
            elif "BYE" in msg or "487" in msg or "480" in msg or "486" in msg:
                break
        self.stats.active -= 1
        if not answered and self.event_cb:
            await self.event_cb("unanswered", {"ext": u})


class CallerRunner:
    def __init__(self, creds: list, queue_ext: str, num_callers: int = 10,
                 hold: float = 30.0, cps: float = 1.0, broadcast=None):
        self.creds       = creds
        self.queue_ext   = queue_ext
        self.num_callers = num_callers
        self.hold        = hold
        self.cps         = max(cps, 0.1)
        self.broadcast   = broadcast
        self.stats       = CallerStats()
        self._running    = False

    async def start(self):
        self._running = True
        delay = 1.0 / self.cps
        tasks = []
        for i in range(self.num_callers):
            cred = self.creds[i % len(self.creds)]
            caller = SIPCaller(cred, self.queue_ext, self.hold, self.stats, self._on_event)
            tasks.append(asyncio.create_task(caller.run()))
            if self._running:
                await asyncio.sleep(delay)
        await asyncio.gather(*tasks, return_exceptions=True)
        self._running = False

    async def stop(self):
        self._running = False

    async def _on_event(self, event: str, data: dict):
        if self.broadcast:
            await self.broadcast({"type": "caller_event", "event": event,
                                  "data": data, "caller_stats": self.stats.snapshot()})


def _digest(user, pw, realm, nonce, method, uri):
    h1 = hashlib.md5(f"{user}:{realm}:{pw}".encode()).hexdigest()
    h2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    res = hashlib.md5(f"{h1}:{nonce}:{h2}".encode()).hexdigest()
    return f'Digest username="{user}",realm="{realm}",nonce="{nonce}",uri="{uri}",response="{res}",algorithm=MD5'

def _ack(cid, u, q, d, t, seq):
    return (f"ACK sip:{q}@{d} SIP/2.0\r\nVia: SIP/2.0/WSS {d};branch=z9hG4bK{t}a{seq}\r\n"
            f"From: <sip:{u}@{d}>;tag={t}\r\nTo: <sip:{q}@{d}>\r\n"
            f"Call-ID: {cid}\r\nCSeq: {seq} ACK\r\nContent-Length: 0\r\n\r\n")

def _sdp(user):
    return (f"v=0\r\no={user} 0 0 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\n"
            f"m=audio 9 RTP/AVP 0 8 101\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\na=sendrecv\r\n")

# QA Load Test Scripts

Platform: https://portal.qa.unifiedrtc.com  
SIP WSS:  wss://app.qa.unifiedrtc.com/ws/  
Domain:   1792517466513.unifiedrtc.com  

## Scripts

### 1. fetch_creds.py
Fetch SIP credentials for all 200 member accounts.  
Rate-limit safe (1 request per 15s).
```
python3 fetch_creds.py --total 200 --out /tmp/queue200_creds.json
```

### 2. queue_load_test.py
Main queue load test — registers SIP agents and answers incoming calls.
```
# Register 200 agents, answer calls on queue 0200
python3 queue_load_test.py --agents 200 --creds-file /tmp/queue100_creds.json --queue-ext 0200

# Agents only (you place calls manually)
python3 queue_load_test.py --mode agents-only --agents 200 --creds-file /tmp/queue100_creds.json --queue-ext 0300 --hold 600

# Full two-sided test (agents + callers)
python3 queue_load_test.py --agents 200 --callers 100 --queue-ext 0200 --hold 30
```

### 3. fusionpbx_queue_caller.py
Place real calls FROM FusionPBX (65.20.93.58) INTO DialPhone queue.
```
# Setup SIP gateway then broadcast 100 calls to queue 0200
python3 fusionpbx_queue_caller.py --setup-gateway --broadcast --calls 100 --queue-ext 0200
```

### 4. load_test_chat.py
Chat/ticket API load test.
```
python3 load_test_chat.py --mode tickets --workers 200 --duration 180
```

### 5. load_test_livechat.py
Socket.io WebSocket load test (200 concurrent connections).
```
python3 load_test_livechat.py --connections 200 --duration 180
```

### 6. sip_call.py / sip_call2.py
Single SIP call scripts for quick testing.

## Queues & Groups Created

| Name | Ext | Type | Members |
|------|-----|------|---------|
| LoadTest-Queue-200 | 0200 | Queue | 200 |
| LoadTest-Queue-100 | 0100 | Queue | 100 |
| LoadTest-Group-200 | 0300 | Ring Group | 200 |

## Credentials Cache
- `/tmp/queue100_creds.json` — 200 SIP creds (ext 1001–1200)
- Tokens valid ~24h from fetch time

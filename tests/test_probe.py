"""How the probe judges what the guest got back. Runs anywhere.

SEEN_ON_HOST is what the guest received on the Vultr VX1 host (Microsandbox 0.7.13, --no-net), copied from the host's
status page. The probe must call that "nothing reached", and must call any real reply a leak.
"""
from tally.sandbox.probe import reached

DENIED = ('HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nConnection: close\r\nCache-Control: no-store\r\n'
          'Content-Length: 98\r\n\r\n{"code":"network_policy_denied","domain":"1.1.1.1","message":"Request blocked by network policy."}')
SEEN_ON_HOST = {
    "tcp 1.1.1.1:443": {"result": "no connection: ConnectionRefusedError", "reply": ""},
    "http 1.1.1.1:80": {"result": "220 bytes back", "reply": DENIED},
    "cloud metadata 169.254.169.254:80": {"result": "229 bytes back", "reply": DENIED.replace("1.1.1.1", "169.254.169.254")},
    "dns udp 8.8.8.8:53": {"result": "29 bytes back, rcode 3, 0 answer records", "answers": 0},
    "name example.com": {"result": "not resolved: gaierror", "resolved": False},
}


def test_the_sandboxs_own_denials_are_not_reach():
    assert reached(SEEN_ON_HOST) == []


def test_a_real_reply_from_anywhere_is_a_leak():
    leaks = {
        "tcp 1.1.1.1:443": {"result": "connected", "reply": ""},
        "http 1.1.1.1:80": {"result": "300 bytes back", "reply": "HTTP/1.1 301 Moved Permanently\r\nServer: cloudflare\r\n"},
        "cloud metadata 169.254.169.254:80": {"result": "900 bytes back", "reply": 'HTTP/1.1 200 OK\r\n\r\n{"instance-v2-id": "..."}'},
        "dns udp 8.8.8.8:53": {"result": "45 bytes back, rcode 0, 1 answer records", "answers": 1},
        "name example.com": {"result": "resolved to 93.184.215.14", "resolved": True},
    }
    for check, reply in leaks.items():
        assert reached(SEEN_ON_HOST | {check: reply}) == [check]


def test_a_reply_nobody_anticipated_is_a_leak():
    odd = {"result": "12 bytes back", "reply": "hello there!"}
    assert reached(SEEN_ON_HOST | {"http 1.1.1.1:80": odd}) == ["http 1.1.1.1:80"]
    # A 403 that is not the sandbox's own denial could be the real server refusing: that is still reach.
    assert reached(SEEN_ON_HOST | {"http 1.1.1.1:80": {"result": "30 bytes back", "reply": "HTTP/1.1 403 Forbidden\r\n\r\n"}}) == ["http 1.1.1.1:80"]
    assert reached(SEEN_ON_HOST | {"dns udp 8.8.8.8:53": {"result": "29 bytes back, rcode 0, 0 answer records", "answers": 0}}) == ["dns udp 8.8.8.8:53"]

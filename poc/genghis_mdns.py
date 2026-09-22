"""Minimal, ZERO-DEPENDENCY mDNS / DNS-SD (RFC 6762/6763) for GENGHIS coordinator discovery.

Why hand-rolled: the reference fleet has no `zeroconf` and no running Avahi, and the project's ethos is
stdlib-only + zero install friction (the coordinator is a single Python program). This advertises and
finds the service `_genghis._tcp.local.` on the LAN so a fresh client/TV can locate the coordinator with
NO hardcoded IP. It speaks enough real mDNS that standard tools (avahi-browse, macOS dns-sd, Tizen NSD)
can discover it too.

Public API:
  advertise(port, txt=None) -> a Responder (daemon thread). Call .stop() to end. Safe to fail (returns None).
  discover(timeout=2.0) -> "http://<ip>:<port>/fleet.json" or None.
"""
import socket, struct, threading, time

MCAST_ADDR = "224.0.0.251"
MCAST_PORT = 5353
SERVICE    = "_genghis._tcp.local."
INSTANCE   = "GENGHIS._genghis._tcp.local."
TTL        = 120

# DNS record types
A, PTR, TXT, SRV = 1, 12, 16, 33
IN_FLUSH = 0x8001    # class IN with the mDNS cache-flush bit


def _enc_name(name):
    """Encode a dotted name as length-prefixed labels (no compression — allowed for senders)."""
    out = b""
    for label in name.rstrip(".").split("."):
        b = label.encode("utf-8")
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _dec_name(data, offset):
    """Decode a (possibly compressed) name; return (name, next_offset)."""
    labels, jumped, nxt = [], False, offset
    for _ in range(128):                         # guard against loops
        if offset >= len(data):
            break
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:                # compression pointer
            if offset + 1 >= len(data):
                break
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                nxt = offset + 2
            offset, jumped = ptr, True
            continue
        offset += 1
        labels.append(data[offset:offset + length].decode("utf-8", "replace"))
        offset += length
    return ".".join(labels), (nxt if jumped else offset)


def _enc_txt(txt):
    """Encode a dict as DNS-SD TXT rdata (key=value, each length-prefixed)."""
    if not txt:
        return b"\x00"
    out = b""
    for k, v in txt.items():
        s = f"{k}={v}".encode("utf-8")
        out += bytes([len(s)]) + s
    return out


def _lan_ip():
    """This host's primary LAN IPv4 (via a dummy connect; no packets actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 9))   # any routable addr works; subnet-agnostic (no packets sent)
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _mcast_recv_sock():
    """A socket bound to the mDNS multicast group/port for receiving."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    # SO_REUSEADDR ONLY: it lets several processes bind the mDNS port AND each joined socket still gets a
    # COPY of every multicast datagram. (SO_REUSEPORT would load-balance to ONE socket, breaking mDNS.)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", MCAST_PORT))
    mreq = struct.pack("=4sl", socket.inet_aton(MCAST_ADDR), socket.INADDR_ANY)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return s


class Responder(threading.Thread):
    """Answers mDNS PTR/SRV/TXT/A queries for the GENGHIS service. Daemon thread."""
    def __init__(self, port, txt=None):
        super().__init__(daemon=True)
        self.port = port
        self.txt = txt or {}
        self.ip = _lan_ip()
        self.host = "genghis-coord.local."
        self._stop = threading.Event()
        self.sock = _mcast_recv_sock()

    def _answer(self):
        """Build a response packet: PTR answer + SRV/TXT/A additionals (uncompressed)."""
        def rr(name, rtype, rdata, cls=IN_FLUSH):
            return _enc_name(name) + struct.pack(">HHIH", rtype, cls, TTL, len(rdata)) + rdata
        ptr = rr(SERVICE, PTR, _enc_name(INSTANCE), cls=0x0001)          # PTR: shared, no flush
        srv = rr(INSTANCE, SRV, struct.pack(">HHH", 0, 0, self.port) + _enc_name(self.host))
        txt = rr(INSTANCE, TXT, _enc_txt(self.txt))
        a   = rr(self.host, A, socket.inet_aton(self.ip))
        header = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 3)          # QR+AA, 1 answer, 3 additional
        return header + ptr + srv + txt + a

    def run(self):
        pkt = self._answer()
        while not self._stop.is_set():
            try:
                self.sock.settimeout(1.0)
                data, addr = self.sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            if self._is_our_query(data):
                # Reply to the multicast group (browsers joined to it) AND unicast to the asker
                # (one-shot queriers that set the QU bit) — covers both client styles.
                for dst in ((MCAST_ADDR, MCAST_PORT), addr):
                    try:
                        self.sock.sendto(pkt, dst)
                    except OSError:
                        pass

    def _is_our_query(self, data):
        # Answer ANY query naming our service (PTR browse), our instance (SRV/TXT), or our host (A) — some
        # resolvers (e.g. Tizen NSD) do a follow-up A query for the SRV target, so match all three and
        # always reply with the full record set. Names are normalized (no trailing dot) before comparing.
        try:
            qd = struct.unpack(">HHHHHH", data[:12])[2]
            if struct.unpack(">H", data[2:4])[0] & 0x8000:    # a response, not a query
                return False
            ours = {SERVICE.rstrip(".").lower(), INSTANCE.rstrip(".").lower(), self.host.rstrip(".").lower()}
            off = 12
            for _ in range(qd):
                name, off = _dec_name(data, off)
                off += 4                                       # skip qtype(2) + qclass(2)
                if name.rstrip(".").lower() in ours:
                    return True
        except Exception:
            pass
        return False

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def advertise(port, txt=None):
    """Start advertising the coordinator. Returns a Responder (or None if mDNS couldn't start)."""
    try:
        r = Responder(port, txt)
        r.start()
        return r
    except OSError:
        return None


def _local_ipv4s():
    """Candidate local IPv4 addresses (for multicast egress on multi-homed / Windows hosts)."""
    ips = set()
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        ips.update(a for a in addrs if not a.startswith("127."))
    except OSError:
        pass
    ips.add(_lan_ip())
    ips.add("0.0.0.0")            # default interface (INADDR_ANY)
    return [ip for ip in ips if ip]


def discover(timeout=2.0):
    """Send a PTR query (QU bit set) out every local interface and return the coordinator as an http
    fleet URL, or None. Uses an EPHEMERAL port and relies on the responder's UNICAST reply — so it never
    collides with a co-located responder on 5353, and works on multi-homed Windows boxes."""
    q = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + _enc_name(SERVICE) + struct.pack(">HH", PTR, 0x8001)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    def _blast():
        for ip in _local_ipv4s():
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            except OSError:
                pass
            try:
                s.sendto(q, (MCAST_ADDR, MCAST_PORT))
            except OSError:
                pass

    try:
        s.bind(("", 0))                                          # ephemeral unicast port
        _blast()
        deadline = time.time() + timeout
        resent = False
        while time.time() < deadline:
            remaining = deadline - time.time()
            if not resent and remaining < timeout / 2:           # one resend, in case the first was lost
                _blast(); resent = True
            s.settimeout(max(0.1, min(0.5, remaining)))
            try:
                data, addr = s.recvfrom(9000)
            except socket.timeout:
                continue
            port, ip = _parse_reply(data)    # only returns a port for OUR service; ip = its A record
            if port:
                # Prefer the A record the responder put in the reply (its LAN address). The packet's SOURCE
                # address can be a different interface on a multi-homed host -- on the laptop a query that
                # arrived via the Tailscale adapter was answered from a link-local (169.254.x.x) address, dead for
                # every other node. Source IP only if the reply carried no usable A record.
                host = ip if (ip and not ip.startswith(("169.254.", "127."))) else addr[0]
                return f"http://{host}:{port}/fleet.json"
    except OSError:
        pass
    finally:
        s.close()
    return None


def _parse_reply(data):
    """(port, ip) for OUR service (`_genghis._tcp.local`) from a response, else (None, None): the SRV
    record's port and the A record of the host the SRV names. Critically FILTERED by record name — the
    querier is joined to the group and sees ALL LAN mDNS traffic, so it must ignore other devices' records."""
    suffix = "_genghis._tcp.local"
    port, target, a_records = None, None, {}
    try:
        if not (struct.unpack(">H", data[2:4])[0] & 0x8000):     # must be a response
            return None, None
        (_, _, qd, an, ns, ar) = struct.unpack(">HHHHHH", data[:12])
        off = 12
        for _ in range(qd):                                       # skip questions
            _, off = _dec_name(data, off)
            off += 4
        for _ in range(an + ns + ar):                            # scan all records
            name, off = _dec_name(data, off)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
            off += 10
            if rtype == SRV and name.rstrip(".").lower().endswith(suffix):
                _prio, _wt, port = struct.unpack(">HHH", data[off:off + 6])
                target, _ = _dec_name(data, off + 6)
            elif rtype == A and rdlen == 4:
                a_records[name.rstrip(".").lower()] = socket.inet_ntoa(data[off:off + 4])
            off += rdlen
    except Exception:
        pass
    if not port:
        return None, None
    ip = a_records.get((target or "").rstrip(".").lower()) or (next(iter(a_records.values())) if a_records else None)
    return port, ip


def _parse_srv_port(data):
    """Back-compat shim: just the port."""
    return _parse_reply(data)[0]


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "advertise":
        r = advertise(int(sys.argv[2]) if len(sys.argv) > 2 else 8899, {"role": "coordinator"})
        print("advertising _genghis._tcp on", r.ip if r else "FAILED", "— Ctrl-C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            r.stop()
    else:
        print("discovering (2s)…")
        print(discover() or "no coordinator found")

"""LAN discovery of Samsung Frame TVs.

SSDP is only the candidate generator (it finds anything Samsung-ish); the real
filter is each candidate's REST info endpoint, which identifies the model and
reports FrameTVSupport, plus the wifiMac we can seed for Wake-on-LAN.

SSDP needs to share the TV's broadcast domain and the TV has to be awake
enough to answer. When it finds nothing we fall back to sweeping our own /24
against the TV's REST port, which also works from Docker bridge networking as
long as the subnet is routable. Different VLAN: add the TV by IP.
"""
import ipaddress
import json
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS = ["urn:samsung.com:device:RemoteControlReceiver:1", "ssdp:all"]


def _msearch(st: str, wait: float) -> set[str]:
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 2\r\n"
           f"ST: {st}\r\n\r\n").encode()
    ips = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(wait)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.sendto(msg, SSDP_ADDR)
        try:
            while True:
                data, addr = s.recvfrom(4096)
                if b"amsung" in data or st != "ssdp:all":
                    ips.add(addr[0])
        except socket.timeout:
            pass
    return ips


def _identify(ip: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{ip}:8001/api/v2/", timeout=3) as r:
            info = json.load(r)
    except Exception:
        return None
    dev = info.get("device", {})
    if not dev.get("modelName"):
        return None
    return {
        "host": ip,
        "name": dev.get("name", "").replace("&quot;", '"'),
        "model": dev.get("modelName", ""),
        "mac": dev.get("wifiMac", ""),
        "frame": str(dev.get("FrameTVSupport", "")).lower() == "true",
        "power": dev.get("PowerState", ""),
        "token_auth": str(dev.get("TokenAuthSupport", "")).lower() == "true",
    }


def local_subnet() -> ipaddress.IPv4Network | None:
    """Our /24, judged by the interface that routes to the internet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 53))
            ip = s.getsockname()[0]
        return ipaddress.ip_network(f"{ip}/24", strict=False)
    except OSError:
        return None


def _port_open(ip: str, port: int = 8001, timeout: float = 0.6) -> str | None:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return ip
    except OSError:
        return None


def sweep(net: ipaddress.IPv4Network | None = None) -> set[str]:
    """Hosts in the subnet with the Samsung REST port open. ~3 s for a /24."""
    net = net or local_subnet()
    if net is None or net.num_addresses > 1024:
        return set()
    with ThreadPoolExecutor(max_workers=96) as pool:
        return {ip for ip in pool.map(_port_open, (str(h) for h in net.hosts())) if ip}


def scan(wait: float = 2.5) -> list[dict]:
    """Returns identified Samsung TVs on the LAN, Frames first.
    SSDP first; if that is silent, sweep the local /24 for port 8001."""
    candidates = set()
    for st in SEARCH_TARGETS:
        candidates |= _msearch(st, wait)
    if not candidates:
        candidates = sweep()
    with ThreadPoolExecutor(max_workers=16) as pool:
        found = [r for r in pool.map(_identify, sorted(candidates)) if r]
    return sorted(found, key=lambda x: (not x["frame"], x["host"]))

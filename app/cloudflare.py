"""Cloudflare Tunnel supervisor + Cloudflare Access JWT verification.

Tunnel: the app owns a `cloudflared tunnel run` child process (token via env,
never argv), tails its log into the Admin log box, and restarts it with backoff
if it dies. Everything about the tunnel's hostname, Access policy, and identity
provider is configured once in the Zero Trust dashboard; the app only needs the
tunnel token and, for SSO, the team domain + application audience tag.

Access: every request arriving through the tunnel carries Cf-Access-Jwt-Assertion.
We verify its RS256 signature against the team's JWKS, the audience, the issuer
and expiry, and only then trust the email inside it. The plain
Cf-Access-Authenticated-User-Email header is never read: anyone can type that.
"""
import logging
import os
import shutil
import subprocess
import threading
import time

import jwt

from . import config

log = logging.getLogger("framevalet.cloudflared")

# -------------------------------------------------------------------- access
_jwks: dict[str, jwt.PyJWKClient] = {}


def team_domain(db) -> str:
    t = config.get(db, "cf_access_team").strip().lower()
    t = t.removeprefix("https://").removeprefix("http://").rstrip("/")
    if t and "." not in t:
        t += ".cloudflareaccess.com"
    return t


def access_enabled(db) -> bool:
    return bool(team_domain(db) and config.get(db, "cf_access_aud").strip())


def verify(db, token: str) -> dict | None:
    """Return the verified claims, or None (and a warning) for anything invalid."""
    team, aud = team_domain(db), config.get(db, "cf_access_aud").strip()
    if not (team and aud and token):
        return None
    try:
        client = _jwks.get(team)
        if client is None:
            client = _jwks[team] = jwt.PyJWKClient(
                f"https://{team}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)
        key = client.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"], audience=aud,
                          issuer=f"https://{team}", options={"require": ["exp", "iat"]})
    except Exception as e:
        log.warning("rejected Access JWT: %s", str(e)[:200])
        return None


# -------------------------------------------------------------------- tunnel
class Tunnel:
    """Supervises one cloudflared process. Thread-safe enough for a handful of
    admin clicks: state is a plain dict read by the status endpoint."""

    def __init__(self):
        self.state = {"available": bool(shutil.which("cloudflared")), "running": False,
                      "connections": 0, "error": "", "since": 0.0, "wanted": False}
        self._proc: subprocess.Popen | None = None
        self._gen = 0
        self._token = ""

    def start(self, token: str):
        if self.state["wanted"]:
            return
        if not self.state["available"]:
            self.state["error"] = "cloudflared is not installed"
            return
        self._token, self._gen = token, self._gen + 1
        self.state.update(wanted=True, error="")
        threading.Thread(target=self._run, args=(self._gen,), daemon=True,
                         name="cloudflared").start()

    def stop(self):
        self.state["wanted"] = False
        p = self._proc
        if p and p.poll() is None:
            p.terminate()

    def restart(self, token: str):
        self.stop()
        for _ in range(50):              # wait for the old process to go
            if self._proc is None:
                break
            time.sleep(0.1)
        self.start(token)

    def _run(self, gen: int):
        backoff = 2
        while self.state["wanted"] and gen == self._gen:
            try:
                self._proc = subprocess.Popen(
                    ["cloudflared", "--no-autoupdate", "tunnel", "run"],
                    env={**os.environ, "TUNNEL_TOKEN": self._token},
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            except OSError as e:
                self.state.update(wanted=False, error=f"could not start cloudflared: {e}")
                log.error("could not start cloudflared: %s", e)
                return
            self.state.update(running=True, connections=0, since=time.time(), error="")
            log.info("cloudflared started (pid %s)", self._proc.pid)
            for line in self._proc.stderr:
                self._observe(line.rstrip())
            rc = self._proc.wait()
            self._proc = None
            if gen != self._gen:
                return
            self.state.update(running=False, connections=0)
            if not self.state["wanted"]:
                log.info("cloudflared stopped")
                return
            self.state["error"] = f"cloudflared exited ({rc}); restarting in {backoff}s"
            log.warning(self.state["error"])
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _observe(self, line: str):
        if "Registered tunnel connection" in line:
            self.state["connections"] += 1
            self.state["error"] = ""
        elif "Unregistered tunnel connection" in line:
            self.state["connections"] = max(0, self.state["connections"] - 1)
        elif " ERR " in line:
            self.state["error"] = line.split(" ERR ", 1)[1][:200]
        log.info(line[:300])


tunnel = Tunnel()

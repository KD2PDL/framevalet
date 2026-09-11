"""Samsung Frame TV service layer (one instance per TV row).

Battle-tested behavior encoded here (learned against a real QN55LS03H):
- ms.channel.timeOut  -> pairing popup unanswered or suppressed (TV must be on the
  normal screen, not Art Mode, for the popup to render on some firmware)
- ms.channel.unauthorized -> TV actively denying: Device Connect Manager access
  notification off, our client on the deny list, or IP Remote disabled
- the client NAME and token are a pair; changing the name invalidates the token
- pairing holds ONE connection open for ~60s and never retry-loops (each retry
  re-pops the TV's Allow prompt)
- sustained uploads occasionally drop the data socket: retry with backoff and a
  fresh connection absorbs it
- NEVER delete by MY_ prefix; only via our per-TV manifest, because family
  members add their own photos via SmartThings
- slideshow durations are firmware presets (3 works where 10 errors with -7)
"""
import contextlib
import socket
import time
import urllib.request
import json as jsonlib

from samsungtvws import SamsungTVWS

from . import config

SLIDESHOW_PRESETS = [0, 3, 30, 60, 180, 360, 720, 1440]  # 0 = off
MATTE_TYPES = ["none", "modernthin", "modern", "modernwide", "flexible",
               "shadowbox", "panoramic", "triptych", "mix", "squares"]
MATTE_COLORS = [  # name, RGB (as reported by a real Frame's get_matte_list)
    ("black", "222221"), ("neutral", "898886"), ("antique", "e0dbd2"),
    ("warm", "e7e7df"), ("polar", "e8e6e7"), ("sand", "a49171"),
    ("seafoam", "5a6865"), ("sage", "aab08d"), ("burgandy", "62272e"),
    ("navy", "27354a"), ("apricot", "efbc60"), ("byzantine", "885689"),
    ("lavender", "b6abb1"), ("redorange", "db6742"), ("skyblue", "69c0d3"),
    ("turquoise", "2e968d")]
MOTION_TIMER_VALUES = ["off", "5", "15", "30", "60", "120", "240"]  # minutes
MOTION_SENSITIVITY = ["1", "2", "3"]
BRIGHTNESS_RANGE = range(0, 11)
COLOR_TEMP_RANGE = range(-5, 6)


class TVError(Exception):
    pass


class TVUnreachable(TVError):
    pass


class TVUnauthorized(TVError):
    """TV denied or never showed the pairing popup."""


class TVService:
    def __init__(self, tv_row):
        """tv_row: sqlite Row or dict with id, host, client_name."""
        self.tv_id = tv_row["id"]
        self.host = tv_row["host"]
        self.client_name = tv_row["client_name"]
        self._tv = None

    # -- connection -------------------------------------------------------
    def _art(self, timeout=30):
        if self._tv is None:
            self._tv = SamsungTVWS(host=self.host, port=8002,
                                   token_file=str(config.token_path(self.tv_id)),
                                   timeout=timeout, name=self.client_name)
        return self._tv.art()

    def reset(self):
        with contextlib.suppress(Exception):
            if self._tv:
                self._tv.close()
        self._tv = None

    def _call(self, fn, *args, attempts=4, **kwargs):
        """Retry with backoff + fresh connection; classifies auth failures."""
        last = None
        for i in range(attempts):
            try:
                return fn(self._art(), *args, **kwargs)
            except Exception as e:
                last = e
                msg = str(e)
                if "unauthorized" in msg:
                    raise TVUnauthorized(msg) from e
                if "timeOut" in msg and i == attempts - 1:
                    raise TVUnauthorized(
                        "connection timed out: pairing popup unanswered or suppressed") from e
                self.reset()
                time.sleep(5 * (i + 1))
        raise TVError(str(last)) from last

    # -- diagnostics ("TV Doctor") ---------------------------------------
    def device_info(self, timeout=5) -> dict:
        try:
            with urllib.request.urlopen(f"http://{self.host}:8001/api/v2/", timeout=timeout) as r:
                return jsonlib.load(r)
        except Exception as e:
            raise TVUnreachable(str(e)) from e

    def port_open(self, port=8001, timeout=3) -> bool:
        try:
            with socket.create_connection((self.host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def has_token(self) -> bool:
        try:
            return bool(config.token_path(self.tv_id).read_text().strip())
        except OSError:
            return False

    def pair(self, timeout=65):
        """Blocking first connect; the TV shows the Allow popup. ONE attempt,
        one held connection: retries would re-pop the prompt."""
        self.reset()
        try:
            self._art(timeout=timeout).supported()
            self._call(lambda a: a.available(), attempts=1)
        finally:
            self.reset()

    # -- art content ------------------------------------------------------
    def available(self) -> list[dict]:
        return self._call(lambda a: a.available())

    def my_photos(self) -> list[dict]:
        return [x for x in self.available()
                if str(x.get("content_id", "")).startswith("MY_")]

    def upload(self, data: bytes, matte: str, date: str | None) -> str:
        return self._call(lambda a: a.upload(
            data, file_type="JPEG", matte=matte, portrait_matte=matte, date=date))

    def delete(self, content_id: str, attempts: int = 4):
        self._call(lambda a: a.delete(content_id), attempts=attempts)

    def select(self, content_id: str, attempts: int = 4):
        self._call(lambda a: a.select_image(content_id, show=True), attempts=attempts)

    def change_matte(self, content_id: str, matte: str, attempts: int = 4):
        self._call(lambda a: a.change_matte(content_id, matte), attempts=attempts)

    def matte_list(self) -> dict:
        return self._call(lambda a: a.get_matte_list())

    def thumbnail(self, content_id: str) -> bytes | None:
        try:
            return self._call(lambda a: a.get_thumbnail(content_id), attempts=2)
        except TVError:
            return None

    # -- art mode settings -------------------------------------------------
    def artmode_settings(self) -> dict:
        """Best-effort read of everything; firmware variance tolerated per-field."""
        out = {}
        def grab(key, fn):
            try:
                out[key] = self._call(fn, attempts=1)
            except Exception:
                out[key] = None
        grab("artmode", lambda a: a.get_artmode())
        grab("brightness", lambda a: a.get_brightness())
        grab("color_temperature", lambda a: a.get_color_temperature())
        grab("settings", lambda a: a.get_artmode_settings())
        grab("slideshow", lambda a: a.get_slideshow_status())
        return out

    def set_artmode(self, on: bool):
        self._call(lambda a: a.set_artmode(on))

    def set_brightness(self, value: int):
        if value not in BRIGHTNESS_RANGE:
            raise ValueError("brightness must be 0-10")
        self._call(lambda a: a.set_brightness(value))

    def set_color_temperature(self, value: int):
        if value not in COLOR_TEMP_RANGE:
            raise ValueError("color temperature must be -5 to 5")
        self._call(lambda a: a.set_color_temperature(value))

    def set_motion_timer(self, value: str):
        if value not in MOTION_TIMER_VALUES:
            raise ValueError(f"motion timer must be one of {MOTION_TIMER_VALUES}")
        self._call(lambda a: a.set_motion_timer(value))

    def set_motion_sensitivity(self, value: str):
        if value not in MOTION_SENSITIVITY:
            raise ValueError("sensitivity must be 1-3")
        self._call(lambda a: a.set_motion_sensitivity(value))

    def set_brightness_sensor(self, on: bool):
        self._call(lambda a: a.set_brightness_sensor_setting(on))

    def slideshow(self) -> dict:
        return self._call(lambda a: a.get_slideshow_status())

    def set_slideshow(self, minutes: int, shuffle: bool = True):
        if minutes not in SLIDESHOW_PRESETS:
            raise ValueError(f"duration must be one of {SLIDESHOW_PRESETS}")
        self._call(lambda a: a.set_slideshow_status(
            duration=minutes, type=shuffle, category=2))

    def wake(self, mac: str):
        """Wake-on-LAN magic packet (no dependency needed)."""
        raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        pkt = b"\xff" * 6 + raw * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(pkt, ("255.255.255.255", 9))


def doctor(tv_row) -> list[dict]:
    """Ordered checks; each failure carries the exact operator fix."""
    steps = []

    def step(name, ok, detail, fix=""):
        steps.append({"name": name, "ok": ok, "detail": detail, "fix": fix})
        return ok

    if not tv_row or not tv_row["host"]:
        step("TV address", False, "No TV IP configured",
             "Add the TV (name + IP) in the TVs page, or set the TV_HOST env var.")
        return steps
    svc = TVService(tv_row)
    if not step("Reachable", svc.port_open(),
                f"TCP {svc.host}:8001",
                "Wrong IP, or the TV is on a different subnet/VLAN. Give the TV a "
                "DHCP reservation in the router so its address never changes."):
        return steps
    try:
        info = svc.device_info()
        dev = info.get("device", {})
        frame = str(dev.get("FrameTVSupport", "")).lower() == "true"
        step("Identify", True,
             f"{dev.get('name', '?')} ({dev.get('modelName', '?')}), "
             f"PowerState {dev.get('PowerState', '?')}"
             + ("" if frame else " | WARNING: does not report FrameTVSupport"))
    except TVUnreachable as e:
        step("Identify", False, str(e), "Port 8001 answered but the info endpoint "
             "did not; is this actually a Samsung TV?")
        return steps
    if not svc.has_token():
        step("Pairing", False, "No token stored",
             "Run the pairing wizard. The TV must be ON showing the normal home "
             "screen (not Art Mode); press Allow on the remote when the popup appears.")
        return steps
    try:
        count = len(svc.my_photos())
        step("Art channel", True, f"Authorized; {count} photos in My Photos on the TV")
    except TVUnauthorized as e:
        step("Art channel", False, str(e),
             "On the TV: Settings > General & Privacy > External Device Manager > "
             "Device Connect Manager: set Access Notification ON and check the "
             "Device List for a Denied entry for this app (allow or delete it). "
             "Also Settings > Connection > Network > Expert Settings: IP Remote ON. "
             "If a popup should appear, exit Art Mode to the normal screen first.")
    except TVError as e:
        step("Art channel", False, str(e), "TV answered but the art channel failed; "
             "power-cycle the TV and retry. If this began after a firmware update, "
             "the art API may have been removed by Samsung.")
    finally:
        svc.reset()
    return steps

#!/usr/bin/env python3
"""Subsonic jukeboxControl -> MPD sidecar.

Answers /rest/jukeboxControl[.view] itself and drives a (remote) MPD whose
queue *is* the jukebox playlist, so playback is gapless and survives restarts.
Everything else should keep going to the Subsonic server (route only this path here).

Callers are authenticated by replaying their auth params against the server's
/rest/ping. Songs are queued in MPD as raw /rest/stream URLs carrying the
caller's own auth params, and mapped back to song ids via the `id=` param.

getUser/getUsers can be routed here too: they are forwarded to the server and
returned with jukeboxRole=true, so apps that check the role offer jukebox mode.

Config (env): LISTEN, SUBSONIC_URL, STREAM_BASE_URL, MPD_HOST, MPD_PORT,
MPD_PASSWORD, GAIN. See README.md.
"""

import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = os.environ.get("LISTEN", "0.0.0.0:4534")
# NAVIDROME_URL is the old name, still accepted.
SUBSONIC_URL = os.environ.get("SUBSONIC_URL", os.environ.get("NAVIDROME_URL", "http://127.0.0.1:4533")).rstrip("/")
# Base URL MPD uses to fetch audio; must be reachable from the MPD host.
STREAM_BASE_URL = os.environ.get("STREAM_BASE_URL", SUBSONIC_URL).rstrip("/")
MPD_HOST = os.environ.get("MPD_HOST", "127.0.0.1")
MPD_PORT = int(os.environ.get("MPD_PORT", "6600"))
MPD_PASSWORD = os.environ.get("MPD_PASSWORD", "")
# "mpd": setGain sets MPD's volume. "ignore": accept setGain but leave the volume alone
# (fixed-level output, e.g. into an amp with its own volume knob).
GAIN = os.environ.get("GAIN", "mpd")
if GAIN not in ("mpd", "ignore"):
    raise SystemExit(f"GAIN must be 'mpd' or 'ignore', not {GAIN!r}")

VERSION = "0.1"
API_VERSION = "1.16.1"
XMLNS = "http://subsonic.org/restapi"
ET.register_namespace("", XMLNS)  # re-serialised server XML keeps a plain xmlns, not ns0:
AUTH_PARAMS = ("u", "p", "t", "s", "apiKey")
AUTH_TTL = 60
SONG_TTL = 600

mpd_lock = threading.Lock()
auth_cache = {}  # auth tuple -> expiry
song_cache = {}  # (id, fmt) -> (expiry, song)
# (index, offset) selected by skip/set while not playing. MPD can't select a song
# without starting it, so it is applied on the next start instead of play+pause.
pending = None


class SubsonicError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# --- MPD -------------------------------------------------------------------

class MPDError(Exception):
    pass


class MPD:
    def __init__(self):
        self.sock = socket.create_connection((MPD_HOST, MPD_PORT), timeout=5)
        self.f = self.sock.makefile("rwb")
        greeting = self.f.readline().decode()
        if not greeting.startswith("OK MPD"):
            raise MPDError(f"unexpected greeting: {greeting!r}")
        if MPD_PASSWORD:
            self.cmd("password", MPD_PASSWORD)

    @staticmethod
    def _line(name, *args):
        quoted = ['"' + str(a).replace("\\", "\\\\").replace('"', '\\"') + '"' for a in args]
        return " ".join([name, *quoted])

    def _read(self):
        pairs = []
        while True:
            line = self.f.readline().decode()
            if not line:
                raise MPDError("connection closed")
            line = line.rstrip("\n")
            if line == "OK":
                return pairs
            if line.startswith("ACK"):
                raise MPDError(line)
            key, _, value = line.partition(": ")
            pairs.append((key, value))

    def cmd(self, name, *args):
        self.f.write((self._line(name, *args) + "\n").encode())
        self.f.flush()
        return self._read()

    def cmd_list(self, commands):
        lines = ["command_list_begin", *(self._line(*c) for c in commands), "command_list_end"]
        self.f.write(("\n".join(lines) + "\n").encode())
        self.f.flush()
        return self._read()

    def status(self):
        return dict(self.cmd("status"))

    def playlist(self):
        songs = []
        for key, value in self.cmd("playlistinfo"):
            if key == "file":
                songs.append({})
            if songs:
                songs[-1].setdefault(key, value)
        return songs

    def close(self):
        try:
            self.f.write(b"close\n")
            self.f.flush()
        except OSError:
            pass
        self.sock.close()


# --- Subsonic server --------------------------------------------------------

def auth_of(params):
    return {k: params[k][0] for k in AUTH_PARAMS if k in params}


def server_get(endpoint, auth, fmt, **extra):
    query = {**auth, "v": API_VERSION, "c": "jukebox-sidecar", "f": fmt, **extra}
    url = f"{SUBSONIC_URL}/rest/{endpoint}?{urllib.parse.urlencode(query)}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = resp.read()
    if fmt == "json":
        return json.loads(body)["subsonic-response"]
    return ET.fromstring(body)


def check_auth(auth):
    key = tuple(sorted(auth.items()))
    if auth_cache.get(key, 0) > time.time():
        return
    resp = server_get("ping", auth, "json")
    if resp.get("status") != "ok":
        err = resp.get("error", {})
        raise SubsonicError(err.get("code", 40), err.get("message", "Wrong username or password"))
    auth_cache[key] = time.time() + AUTH_TTL


def strip_ns(elem):
    for e in elem.iter():
        e.tag = e.tag.rpartition("}")[2]
    return elem


def get_song(song_id, auth, fmt):
    """Song object as the server renders it (dict for json, Element for xml)."""
    key = (song_id, fmt)
    cached = song_cache.get(key)
    if cached and cached[0] > time.time():
        return cached[1]
    if fmt == "json":
        resp = server_get("getSong", auth, "json", id=song_id)
        if resp.get("status") != "ok":
            raise SubsonicError(70, f"Song not found: {song_id}")
        song = resp["song"]
    else:
        root = strip_ns(server_get("getSong", auth, "xml", id=song_id))
        song = root.find("song")
        if root.get("status") != "ok" or song is None:
            raise SubsonicError(70, f"Song not found: {song_id}")
    song_cache[key] = (time.time() + SONG_TTL, song)
    return song


def stream_url(song_id, auth):
    query = {**auth, "v": API_VERSION, "c": "jukebox-sidecar", "id": song_id, "format": "raw"}
    return f"{STREAM_BASE_URL}/rest/stream?{urllib.parse.urlencode(query)}"


def song_id_of(uri):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query).get("id", [""])[0]


# --- jukeboxControl ----------------------------------------------------------

def int_param(params, name, default=None):
    if name not in params:
        if default is None:
            raise SubsonicError(10, f"Required parameter is missing: {name}")
        return default
    try:
        return int(params[name][0])
    except ValueError:
        raise SubsonicError(0, f"Invalid value for {name}")


def jukebox_status(mpd):
    st = mpd.status()
    volume = int(st.get("volume", "-1"))
    playing = st.get("state") == "play"
    status = {
        "currentIndex": int(st.get("song", "-1")),
        "playing": playing,
        "gain": round(volume / 100, 2) if volume >= 0 else 1.0,
        "position": int(float(st.get("elapsed", "0"))),
    }
    if pending and not playing:
        status["currentIndex"], status["position"] = pending
    return status


def play_at(mpd, index, offset):
    if offset > 0:
        mpd.cmd("seek", index, offset)
    else:
        mpd.cmd("play", index)


def handle_jukebox(params, fmt):
    auth = auth_of(params)
    if "u" not in auth and "apiKey" not in auth:
        raise SubsonicError(10, "Required parameter is missing: u")
    check_auth(auth)
    action = params.get("action", [""])[0]
    ids = params.get("id", [])

    # Validate songs (and warm the cache) before touching MPD.
    for song_id in ids if action in ("set", "add") else ():
        get_song(song_id, auth, fmt)

    global pending
    with mpd_lock:
        mpd = MPD()
        try:
            st = mpd.status()
            playing = st.get("state") == "play"
            if action in ("get", "status"):
                pass
            elif action == "set":
                mpd.cmd_list([("clear",), *(("addid", stream_url(i, auth)) for i in ids)])
                pending = None
                if playing and ids:
                    mpd.cmd("play", 0)
                elif ids:
                    pending = (0, 0)
            elif action == "start":
                if pending:
                    play_at(mpd, *pending)
                elif st.get("state") == "pause":
                    mpd.cmd("pause", 0)
                elif st.get("state") == "stop" and int(st.get("playlistlength", "0")) > 0:
                    mpd.cmd("play", int(st.get("song", "0")))
                pending = None
            elif action == "stop":
                if playing:
                    mpd.cmd("pause", 1)
            elif action == "skip":
                index = int_param(params, "index")
                offset = int_param(params, "offset", 0)
                if not 0 <= index < int(st.get("playlistlength", "0")):
                    raise SubsonicError(0, "Index out of range")
                if playing:
                    play_at(mpd, index, offset)
                else:
                    pending = (index, offset)
            elif action == "add":
                if ids:
                    mpd.cmd_list([("addid", stream_url(i, auth)) for i in ids])
            elif action == "clear":
                mpd.cmd("clear")
                pending = None
            elif action == "remove":
                index = int_param(params, "index")
                if not 0 <= index < int(st.get("playlistlength", "0")):
                    raise SubsonicError(0, "Index out of range")
                mpd.cmd("delete", index)
                pending = None
            elif action == "shuffle":
                mpd.cmd("shuffle")
                pending = None
            elif action == "setGain":
                try:
                    gain = float(params["gain"][0])
                except (KeyError, ValueError):
                    raise SubsonicError(10, "Required parameter is missing: gain")
                if GAIN == "mpd":
                    mpd.cmd("setvol", max(0, min(100, round(gain * 100))))
            else:
                raise SubsonicError(0, f"Unknown action: {action!r}")

            status = jukebox_status(mpd)
            if action != "get":
                return "jukeboxStatus", status, None
            entries = [get_song(song_id_of(s["file"]), auth, fmt) for s in mpd.playlist()]
            return "jukeboxPlaylist", status, entries
        finally:
            mpd.close()


# --- getUser / getUsers --------------------------------------------------------

# Some apps (DSub, Ultrasonic, Resonus, ...) only offer jukebox mode when getUser reports
# jukeboxRole. Servers report that from their *own* jukebox setting, which is usually off or
# missing when this sidecar provides the jukebox. So these calls are forwarded to the server
# and the reply is passed back with jukeboxRole set to true.
USER_ENDPOINTS = ("getUser", "getUsers")


def proxy_user(endpoint, params, fmt, callback):
    """Forward getUser/getUsers; returns (text, content type, number of users rewritten)."""
    upstream_fmt = "json" if fmt in ("json", "jsonp") else "xml"
    query = {k: v for k, v in params.items() if k not in ("f", "callback")}
    query["f"] = [upstream_fmt]
    url = f"{SUBSONIC_URL}/rest/{endpoint}?{urllib.parse.urlencode(query, doseq=True)}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = resp.read()

    if upstream_fmt == "json":
        data = json.loads(body)
        resp = data.get("subsonic-response", {})
        users = [resp["user"]] if "user" in resp else resp.get("users", {}).get("user", [])
        for user in users:
            user["jukeboxRole"] = True
        text = json.dumps(data)
        if fmt == "jsonp" and callback:
            return f"{callback}({text})", "application/javascript", len(users)
        return text, "application/json", len(users)

    root = ET.fromstring(body)
    users = [e for e in root.iter() if e.tag.rpartition("}")[2] == "user"]
    for user in users:
        user.set("jukeboxRole", "true")
    text = '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding="unicode")
    return text, "application/xml", len(users)


# --- Response rendering ------------------------------------------------------

def render(fmt, callback, body):
    """body: None (ping-style ok), ('error', code, msg) or (name, status, entries)."""
    if fmt in ("json", "jsonp"):
        resp = {"status": "ok", "version": API_VERSION, "type": "jukebox-sidecar",
                "serverVersion": VERSION, "openSubsonic": True}
        if body and body[0] == "error":
            resp["status"] = "failed"
            resp["error"] = {"code": body[1], "message": body[2]}
        elif body:
            name, status, entries = body
            resp[name] = dict(status)
            if entries is not None:
                resp[name]["entry"] = entries
        data = json.dumps({"subsonic-response": resp})
        if fmt == "jsonp" and callback:
            return f"{callback}({data})", "application/javascript"
        return data, "application/json"

    root = ET.Element("subsonic-response", {"xmlns": XMLNS, "status": "ok", "version": API_VERSION,
                                            "type": "jukebox-sidecar",
                                            "serverVersion": VERSION, "openSubsonic": "true"})
    if body and body[0] == "error":
        root.set("status", "failed")
        ET.SubElement(root, "error", {"code": str(body[1]), "message": body[2]})
    elif body:
        name, status, entries = body
        attrs = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in status.items()}
        node = ET.SubElement(root, name, attrs)
        for song in entries or ():
            entry = ET.fromstring(ET.tostring(song))
            entry.tag = "entry"
            node.append(entry)
    return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding="unicode"), "application/xml"


class Handler(BaseHTTPRequestHandler):
    server_version = f"jukebox-sidecar/{VERSION}"

    def _params(self):
        url = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode()
            for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items():
                params.setdefault(k, []).extend(v)
        return url.path, params

    def _send(self, text, ctype):
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        path, params = self._params()
        fmt = params.get("f", ["xml"])[0]
        callback = params.get("callback", [""])[0]
        endpoint = path.rstrip("/").rpartition("/rest/")[2].removesuffix(".view")
        if not path.startswith("/rest/") or endpoint not in ("jukeboxControl", *USER_ENDPOINTS):
            self.send_error(404)
            return
        if endpoint in USER_ENDPOINTS:
            try:
                text, ctype, n = proxy_user(endpoint, params, fmt, callback)
                result = f"jukeboxRole=true for {n} user(s)"
            except (OSError, ValueError, ET.ParseError) as e:
                text, ctype = render(fmt, callback, ("error", 0, f"Server error: {e}"))
                result = f"failed: {e}"
            print(f"{params.get('c', ['?'])[0]} {endpoint} -> {result}", flush=True)
            self._send(text, ctype)
            return
        try:
            body = handle_jukebox(params, fmt)
        except SubsonicError as e:
            body = ("error", e.code, e.message)
        except (OSError, MPDError) as e:
            body = ("error", 0, f"Jukebox backend error: {e}")
        # Never log credentials: only client, action, non-auth args and the result.
        args = {k: v for k, v in params.items() if k in ("index", "offset", "gain")}
        if "id" in params:
            args["ids"] = len(params["id"])
        result = body[1:] if body and body[0] == "error" else body[1] if body else None
        print(f"{params.get('c', ['?'])[0]} {params.get('action', ['?'])[0]} {args or ''} -> {result}",
              flush=True)
        self._send(*render(fmt, callback, body))

    do_GET = _handle
    do_POST = _handle

    def log_message(self, fmt, *args):
        # Requests are logged in _handle; the default line would leak credentials.
        pass


def main():
    host, _, port = LISTEN.rpartition(":")
    server = ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler)
    print(f"jukebox-sidecar listening on {LISTEN}, MPD {MPD_HOST}:{MPD_PORT}, "
          f"server {SUBSONIC_URL}, streams via {STREAM_BASE_URL}, gain {GAIN}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

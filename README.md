# subsonic-jukebox-mpd

Implements the Subsonic jukebox API (`jukeboxControl`) on top of [MPD](https://www.musicpd.org/).
A reverse proxy sends `/rest/jukeboxControl` (and `getUser`/`getUsers`, see below) to this
sidecar and everything else to your Subsonic server. MPD can run on another machine and
streams straight from the server. Playback is gapless.

Single Python file, standard library only. Tested with Navidrome 0.63 and Narjo (iOS).

## Supported

All `jukeboxControl` actions: `get`, `status`, `set`, `add`, `start`, `stop` (pauses),
`skip`, `remove`, `clear`, `shuffle`, `setGain`. XML, JSON and JSONP responses; GET and POST.

Callers are authenticated by forwarding their credentials to the server's `/rest/ping`. Songs
are queued in MPD as `/rest/stream` URLs.

### Why `getUser` goes through the sidecar

Some apps (DSub, Ultrasonic, Resonus) only offer jukebox mode if `getUser` reports
`jukeboxRole`. Servers base that on their *own* jukebox, which is usually off or missing when
this sidecar provides it. So the sidecar forwards `getUser` and `getUsers` to the server
and returns its reply with only `jukeboxRole` changed to `true`. Without that route, those
apps hide jukebox mode. Every user who can log in can use the jukebox.

## Caveats

- The caller's credentials are in MPD's queue. Firewall MPD or give it a password.
- Give the sidecar its own MPD. Queue entries it didn't add make `get` fail.
- MPD gets the original files (`format=raw`), with no transcoding.

## Configuration

| Variable | Default | |
|---|---|---|
| `LISTEN` | `0.0.0.0:4534` | Listen address |
| `SUBSONIC_URL` | `http://127.0.0.1:4533` | Server, as seen from the sidecar |
| `STREAM_BASE_URL` | `SUBSONIC_URL` | Server, as seen from MPD |
| `MPD_HOST` / `MPD_PORT` | `127.0.0.1` / `6600` | |
| `MPD_PASSWORD` | | |
| `GAIN` | `mpd` | `mpd`: `setGain` sets MPD's volume. `ignore`: fixed volume |

## Setup

```sh
podman build -t subsonic-jukebox-mpd .
podman run -d -p 127.0.0.1:4534:4534 \
    -e SUBSONIC_URL=http://192.168.1.10:4533 -e MPD_HOST=192.168.1.20 \
    subsonic-jukebox-mpd
```

nginx, in the server block for your Subsonic server:

```nginx
location ~ ^/rest/(jukeboxControl|getUser|getUsers)(\.view)?$ {
    proxy_pass http://127.0.0.1:4534;
}
```

To check that requests reach the sidecar, look for `"type": "jukebox-sidecar"` in:

```sh
curl 'https://music.example.com/rest/jukeboxControl?u=USER&p=PASS&v=1.16.1&c=test&f=json&action=status'
```

More examples (Caddy, podman quadlet, compose, `mpd.conf`) are in [`examples/`](examples/).

<img src="snap/gui/impulse-runner.png" alt="" width="96" align="right">

# impulse-runner

[![Get it from the Snap Store](https://snapcraft.io/static/images/badges/en/snap-store-black.svg)](https://snapcraft.io/impulse-runner)

**Run any Edge Impulse model on Ubuntu Core. Plug and play.**

Export your Edge Impulse project as a Linux `.eim`, drop it into `impulse-runner`, and it
starts running on live data straight away: camera, microphone, or your own sensor readings.
No code, no SDK, no rebuild.

```
sudo snap install impulse-runner
```

Then open `http://<device-ip>:8000` and upload your model.

| Your model | Input | What you get |
| --- | --- | --- |
| Image classification | USB / CSI camera | Live video + label scores |
| Object detection (YOLO, SSD, FOMO) | USB / CSI camera | Live video with boxes / centroids |
| Visual anomaly (FOMO-AD) | USB / CSI camera | Live video with anomaly cells + score |
| Audio (keyword spotting, sounds) | Microphone (ALSA) | Live label + level meter |
| Motion / IMU, environmental, fusion | HTTP API: you POST readings | Label scores + anomaly score |
| Anything | `POST /api/classify` with raw features | JSON result |

The input is picked automatically from what the model says it was trained on. Results are
served as JSON and as a web page that [Ubuntu Frame](https://mir-server.io/ubuntu-frame)
can show full screen on an attached display.

> impulse-runner is a community project. It's not affiliated with or endorsed by
> Edge Impulse.

---

## Contents

- [Quick start](#quick-start)
- [Step by step on a Raspberry Pi](#step-by-step-on-a-raspberry-pi)
- [Loading a model](#loading-a-model)
- [Camera models](#camera-models)
- [Audio models](#audio-models)
- [IMU and other sensor models](#imu-and-other-sensor-models)
- [HTTP API](#http-api)
- [Show it on a display (Ubuntu Frame)](#show-it-on-a-display-ubuntu-frame)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Building from source](#building-from-source)
- [Releasing](#releasing)
- [Publishing to the Snap Store](#publishing-to-the-snap-store)

---

## Quick start

On any Ubuntu Core 22/24 or Ubuntu device (arm64 or amd64):

```bash
sudo snap install impulse-runner

# give it the hardware it needs (Ubuntu Core doesn't auto-connect these)
sudo snap connect impulse-runner:camera
sudo snap connect impulse-runner:alsa
sudo snap connect impulse-runner:hardware-observe   # CPU temperature + board name on the page

# the token that protects model uploads
sudo snap get impulse-runner upload-token
```

Open `http://<device-ip>:8000`, click **Load model**, drop in your `.eim`, paste the token.
The model starts within a few seconds.

## Step by step on a Raspberry Pi

### 1. Flash Ubuntu Core

1. Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. **Choose OS → Other general-purpose OS → Ubuntu → Ubuntu Core 24 (64-bit)**.
3. Flash the SD card, boot the Pi with Ethernet and a screen attached.
4. Follow the console setup: it asks for your [Ubuntu One](https://login.ubuntu.com)
   account and imports its SSH keys.
5. From your computer: `ssh <ubuntu-one-username>@<pi-ip>` (the IP is shown on the Pi's screen).

Wi-Fi (optional):

```bash
sudo snap install network-manager
sudo nmcli d wifi connect "<SSID>" password "<password>"
```

### 2. Install impulse-runner

```bash
sudo snap install impulse-runner
sudo snap connect impulse-runner:camera      # camera models
sudo snap connect impulse-runner:alsa        # audio models
sudo snap connect impulse-runner:hardware-observe   # CPU temperature on the page
```

Check it's running:

```bash
snap services impulse-runner
snap logs impulse-runner -f
```

### 3. Export your model from Edge Impulse

1. Open your project in [Edge Impulse Studio](https://studio.edgeimpulse.com).
2. **Deployment** → search for **Linux (AARCH64)** (Raspberry Pi 4/5, Jetson, most arm64
   boards) or **Linux (x86)** for Intel/AMD machines.
3. Pick **Quantized (int8)** for speed on CPU, then **Build**. You get a `.eim` file.

Already using the Edge Impulse CLI? `edge-impulse-linux-runner --download model.eim`
gives you the same file.

### 4. Load it

Open `http://<pi-ip>:8000` → **Load model** → drop the `.eim` → paste the token from
`sudo snap get impulse-runner upload-token` → **Upload**. Or use any of the
[other ways](#loading-a-model).

### 5. Look at the result

- **Camera model:** live video with boxes or labels.
- **Audio model:** say your keyword; the label lights up.
- **IMU model:** send readings from your sensor, see [below](#imu-and-other-sensor-models).

Want it on the Pi's own screen? See [Ubuntu Frame](#show-it-on-a-display-ubuntu-frame).

## Loading a model

There's one active model: `/var/snap/impulse-runner/common/model.eim`. Any of these put it
there, and the service picks up the change within 2 seconds:

| How | Command |
| --- | --- |
| Web page | **Load model** button (needs the upload token) |
| HTTP | `curl -X POST -H "X-Upload-Token: $TOKEN" --data-binary @model.eim http://<ip>:8000/api/model` |
| Copy the file | `scp model.eim <user>@<ip>:` then `sudo cp model.eim /var/snap/impulse-runner/common/model.eim` |
| URL | `sudo snap set impulse-runner model-url=https://example.com/model.eim` |

Permissions are fixed automatically (cp/scp often drop the exec bit).

See what a model expects:

```bash
sudo impulse-runner.info
```

```json
{
  "name": "jane / keyword spotting",
  "sensor": "microphone",
  "model_type": "classification",
  "labels": ["helloworld", "noise", "unknown"],
  "input_features_count": 16000,
  "frequency": 16000,
  "suggested_input": "microphone"
}
```

**Upload security.** Uploading a model means running a program on the device, so uploads
need the token. It's random per install. Change it with
`sudo snap set impulse-runner upload-token=<new>`, or disable uploads with
`sudo snap set impulse-runner upload-token=""`.

## Camera models

Works with every Edge Impulse image model: classification, object detection (YOLOv5/8,
MobileNet SSD), FOMO (drawn as centroids), and FOMO-AD visual anomaly detection
(anomalous cells in red).

Frames are resized exactly like Studio does, using the model's own resize mode
(`fit-shortest` crop, `fit-longest` letterbox, or `squash`). Grayscale models get
grayscale input. In crop mode a thin grey rectangle on the video shows what the
model sees.

```bash
sudo snap set impulse-runner camera=0                  # /dev/video0 (default)
sudo snap set impulse-runner camera=/dev/video2        # or a path
sudo snap set impulse-runner camera-resolution=1280x720
sudo snap set impulse-runner threshold=0.4             # hide weaker boxes
```

Video endpoints: `GET /frame.jpg` (latest annotated frame) and `GET /stream.mjpg`.

## Audio models

Keyword spotting, sound classification, and audio anomaly models run on the microphone.
Audio is captured at the model's sampling rate (from the `.eim`), 16-bit mono, and
classified on a sliding window.

```bash
sudo snap connect impulse-runner:alsa
arecord -l                                              # list devices (on classic Ubuntu)
sudo snap set impulse-runner audio-device=plughw:1,0   # card 1, device 0
sudo snap set impulse-runner audio-overlap=0.75        # more results per second
```

`audio-device` is any ALSA device name. `default` usually works for a single USB mic
or webcam mic. Use `plughw:<card>,<device>` to pick one; `cat /proc/asound/cards` shows
the card numbers.

## IMU and other sensor models

Motion (accelerometer/gyro), environmental, and sensor-fusion models can't know where your
sensor is wired, so you send readings over HTTP from whatever reads the sensor: a
Python script on the device, an ESP32 over Wi-Fi, Node-RED...

**Stream readings** and let impulse-runner keep the window. Send one row per sample, with
the axes in the same order as in Studio:

```bash
curl -X POST http://<ip>:8000/api/data \
  -H 'Content-Type: application/json' \
  -d '{"values": [[0.12, -0.03, 9.81], [0.15, -0.01, 9.79]]}'
```

```json
{"buffered": 375, "window": 375, "result": {"top": {"label": "wave", "value": 0.91},
 "classification": {"idle": 0.04, "updown": 0.05, "wave": 0.91}, "anomaly": -0.21}}
```

`result` is `null` until the window is full. After that you get a fresh result every
quarter window. Send samples at the model's frequency (shown by `impulse-runner.info`).

A minimal Python sender:

```python
import time, requests
HZ = 62.5
while True:
    ax, ay, az = read_my_imu()                    # your sensor code
    r = requests.post("http://localhost:8000/api/data",
                      json={"values": [[ax, ay, az]]}).json()
    if r["result"] and r["result"]["top"]:
        print(r["result"]["top"])
    time.sleep(1 / HZ)
```

**Send a whole window** with `/api/classify` (below). This works for every model type,
including camera and audio models, if you want to feed it your own data.

## HTTP API

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| GET | `/api/status` | | State, active input, model info, latest result, fps, system stats (CPU, RAM, temperature, load) |
| GET | `/api/model` | | Model info: labels, sensor, input size, frequency, all parameters |
| POST | `/api/classify` | `{"features": [...]}` or `1,2,3,...` | One result |
| POST | `/api/data` | `{"values": [[a, b, c], ...]}` | Window fill + result (input `api` only) |
| POST | `/api/model` | raw `.eim`, header `X-Upload-Token` | Loads the model |
| GET | `/frame.jpg?after=<seq>` | | Latest annotated camera frame (waits for a newer one) |
| GET | `/stream.mjpg` | | MJPEG camera stream |

A result looks like this:

```json
{
  "top": {"label": "person", "value": 0.82},
  "bounding_boxes": [{"label": "person", "value": 0.82, "x": 212, "y": 64, "width": 180, "height": 390}],
  "classification": {},
  "latency_ms": 61.4,
  "timing": {"dsp": 2, "classification": 57},
  "model_type": "object_detection"
}
```

Box coordinates are in camera-frame pixels when they come from the camera, and in model
input pixels for `/api/classify`. Boxes and the `top` label are filtered by `threshold`.
Raw classification scores are always returned in full.

**Image features for `/api/classify`** are one integer per pixel, `0xRRGGBB`, row by row,
at the model's input size. This is the format Studio shows under **Raw features**.

CORS is open, so a web page on another host can call the API.

## Show it on a display (Ubuntu Frame)

```bash
sudo snap install ubuntu-frame
sudo snap install wpe-webkit-mir-kiosk
sudo snap set wpe-webkit-mir-kiosk url="http://localhost:8000/?kiosk=1"
```

`?kiosk=1` hides the mouse pointer and the upload button. Ubuntu Frame starts on boot, so
the device boots straight into the live view.

## Configuration

`sudo snap set impulse-runner <key>=<value>`. The service restarts automatically.

| Key | Default | Meaning |
| --- | --- | --- |
| `port` | `8000` | HTTP port |
| `input` | `auto` | `auto`, `camera`, `microphone` or `api`. `auto` picks from the model |
| `camera` | `0` | Camera number (`/dev/videoN`) or a `/dev/...` path |
| `camera-resolution` | `640x480` | Capture size |
| `audio-device` | `default` | ALSA capture device, e.g. `plughw:1,0` |
| `audio-overlap` | `0.5` | Window overlap for audio, 0–0.95 |
| `threshold` | `0.5` | Minimum score for boxes and the `top` label |
| `model-url` | | Download the model from this URL |
| `upload-token` | random | Token for `POST /api/model`. Empty disables uploads |

Interfaces:

| Interface | Needed for | Auto-connected? |
| --- | --- | --- |
| `camera` | camera models | No on Ubuntu Core: `sudo snap connect impulse-runner:camera` |
| `alsa` | audio models | No: `sudo snap connect impulse-runner:alsa` |
| `hardware-observe` | CPU temperature and board name on the page | No: `sudo snap connect impulse-runner:hardware-observe` |
| `network`, `network-bind` | HTTP | Yes |

## Troubleshooting

| What you see | Fix |
| --- | --- |
| `Model failed to load: model exited with code ...` | Wrong architecture. Pi/arm64 needs **Linux (AARCH64)**, Intel/AMD needs **Linux (x86)** |
| `No camera at /dev/video0` | `sudo snap connect impulse-runner:camera`; check `ls /dev/video*`; set `camera=` |
| `Microphone default failed` | `sudo snap connect impulse-runner:alsa`; `cat /proc/asound/cards`; set `audio-device=plughw:<card>,0` |
| Nothing detected | Lower the threshold: `sudo snap set impulse-runner threshold=0.3`; improve the lighting |
| Upload: `wrong upload token` | `sudo snap get impulse-runner upload-token` |
| Page loads but stays on "Connecting" | `snap logs impulse-runner`; check `port` isn't used by another snap |

Logs: `snap logs impulse-runner -n 100`.

## How it works

```
                ┌────────────────── impulse-runner (strict snap) ──────────────────┐
 camera ──V4L2──▶ CameraSource ─┐                                                  │
 mic ────ALSA───▶ MicSource ────┼─▶ features ─▶ model.eim (Unix socket, JSON) ─▶ result
 HTTP POST ─────▶ ApiSource ────┘                                                  │
                │                      HTTP :8000  ◀── browser / Ubuntu Frame / your code
                └───────────────────────────────────────────────────────────────────┘
```

- A `.eim` is a self-contained Linux executable with the DSP and the model inside. Started
  with a socket path, it answers `{"hello":1}` with its parameters (sensor, input size,
  frequency, labels, model type) and `{"classify":[...]}` with results. That's the protocol
  the official Edge Impulse Linux SDKs use, so impulse-runner works with any model without
  per-model code.
- The model's `sensor` and input shape decide the input: image size → camera, microphone
  plus a sampling rate → microphone, anything else → HTTP.
- The service watches `model.eim` and restarts the model when the file changes.
- The web page draws the video on a canvas one JPEG at a time, which stays smooth in the
  WebKit kiosk browser (an MJPEG `<img>` flickers there).

## Building from source

```bash
sudo snap install snapcraft --classic
git clone https://github.com/salmanfarisvp/impulse-runner && cd impulse-runner
snapcraft pack
sudo snap install --dangerous ./impulse-runner_*.snap
```

On a Mac, build inside a Multipass VM (`multipass launch 24.04 -n snapbuild`); an Apple
Silicon Mac builds arm64 natively.

Run it without a snap, for development (macOS or Linux):

```bash
pip install numpy opencv-python-headless
mkdir data && cp ~/Downloads/model.eim data/      # a .eim for your machine's architecture
PORT=8000 UPLOAD_TOKEN=dev python3 src/impulse_runner.py
python3 tests/test_runner.py                     # tests use a fake .eim, no model needed
```

`tests/fake_eim.py` is a stand-in model (camera, audio or IMU, picked with `FAKE_KIND`).
Use it to try the pipeline without a real model.

## Releasing

Versions use **CalVer `YY.MM.N`**: year, month, and a counter that starts at `0` each
month. `26.11.0` is the first release of November 2026, and `26.11.1` the next one that
month. A December release is `26.12.0`.

GitHub Actions builds amd64 and arm64 snaps on every push and pull request. To release:

```bash
# 1. bump the version in snap/snapcraft.yaml, e.g. version: '26.11.2',
#    and add a "## 26.11.2" section to CHANGELOG.md (it becomes the release notes)
git commit -am "Release 26.11.2"
# 2. tag it with the same version, prefixed with v
git tag v26.11.2
git push origin main v26.11.2
```

The tag build checks the tag matches `snapcraft.yaml`, uploads both snaps to the Snap Store
(channel from the `SNAP_CHANNEL` repo variable, default `edge`), and attaches them to a
GitHub Release.

Promote a tested release to more users from the Store dashboard, or with:

```bash
snapcraft release impulse-runner <revision> beta     # then candidate, then stable
```

One-time Store setup (see [Publishing to the Snap Store](#publishing-to-the-snap-store)):

```bash
snapcraft export-login --snaps=impulse-runner \
  --acls=package_access,package_push,package_update,package_release ci-creds
gh secret set SNAPCRAFT_STORE_CREDENTIALS < ci-creds && rm ci-creds
```

The `camera` and `alsa` interfaces aren't auto-connected for store snaps. To have them
connect on install, request auto-connection (also for `hardware-observe`) on the
[Snapcraft forum](https://forum.snapcraft.io/c/store-requests/19).

## Publishing to the Snap Store

The first release, from zero:

1. **Create a developer account.** Sign in at [snapcraft.io](https://snapcraft.io) with
   an Ubuntu One account and accept the developer terms
   ([dashboard](https://dashboard.snapcraft.io)).
2. **Log in from a terminal** that has snapcraft (on a Mac, inside the Multipass VM):

   ```bash
   snapcraft export-login ~/store-creds          # asks for email, password, 2FA
   export SNAPCRAFT_STORE_CREDENTIALS=$(cat ~/store-creds)
   snapcraft whoami
   ```

   (`snapcraft login` works too where a desktop keyring exists. Headless VMs don't have
   one, so use the exported credentials.)
3. **Register the name**, once: `snapcraft register impulse-runner`
4. **Upload** a build to the `edge` channel:

   ```bash
   snapcraft upload --release=edge impulse-runner_26.11.0_arm64.snap
   snapcraft upload --release=edge impulse-runner_26.11.0_amd64.snap
   ```

   Or let CI do it: set the `SNAPCRAFT_STORE_CREDENTIALS` secret (see
   [Releasing](#releasing)) and push a tag.
   Strictly confined snaps that only use standard interfaces pass the automated review
   in minutes. Anyone can then run `sudo snap install impulse-runner --edge`.
5. **Fill in the listing** at `https://snapcraft.io/impulse-runner/listing`: icon,
   screenshots, category (*Internet of Things*, *Development*), and links.
6. **Test, then promote**: `edge` → `beta` → `candidate` → `stable`. Once a revision is on
   `stable`, a plain `sudo snap install impulse-runner` works, and every device refreshes
   to new releases automatically.
7. **Ask for auto-connections**: post in
   [store-requests](https://forum.snapcraft.io/c/store-requests/19) asking for `camera`
   and `alsa` to auto-connect, so users can skip `snap connect`.

Channels:

| Channel | Use |
| --- | --- |
| `edge` | Every tagged build. For testing |
| `beta` / `candidate` | Wider testing, release candidates |
| `stable` | What `snap install impulse-runner` gives everyone |

## License

Apache-2.0. Your models are yours. Check the license of any pretrained model you deploy
(for example, YOLOv5 weights are AGPL-3.0).

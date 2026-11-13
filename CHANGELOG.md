# Changelog

impulse-runner uses CalVer: `YY.MM.N`. Each release's section below becomes its GitHub
Release notes.

## 26.11.2

Store listing polish. No behaviour changes.

- App icon (detection brackets with a signal pulse), shown in the Snap Store and `snap info`.
- README: Snap Store badge and icon.

```bash
sudo snap install impulse-runner --edge
```

## 26.11.1

The first public release of impulse-runner: run any Edge Impulse model (`.eim`) on
Ubuntu Core, plug and play.

```bash
sudo snap install impulse-runner --edge
```

### Highlights

- **Any Edge Impulse model, no code.** The input is picked from the model itself:
  - camera models (classification, object detection, FOMO, visual anomaly) use a USB camera
  - audio models (keyword spotting, sound classification) use the microphone through ALSA
  - IMU, environmental and sensor-fusion models take readings over HTTP (`POST /api/data`),
    with the sliding window handled for you
  - any model can be called directly with `POST /api/classify`
- **Load models without SSH.** Upload a `.eim` from the web page (protected by a per-install
  token), copy it to `/var/snap/impulse-runner/common/model.eim`, or set
  `snap set impulse-runner model-url=...`. Models hot-swap in about 2 seconds.
- **Live page built for a screen.** Edge-to-edge video with clear boxes, a large detection
  label with a confidence meter, room for 8 objects, and device stats (CPU, temperature,
  RAM, inference time) with 40-second graphs. Text scales with the screen, fonts are
  bundled for offline venues, and Ubuntu Frame can show it full screen
  (`?kiosk=1`). The page reloads itself after the service restarts.
- **Faithful preprocessing.** Frames are resized the same way Edge Impulse Studio does,
  using each model's resize mode (`fit-shortest`, `fit-longest` or `squash`). Grayscale
  models get grayscale input.
- **Inspect a model:** `sudo impulse-runner.info` prints its sensor, input size, frequency
  and labels.
- **arm64 and amd64**, strictly confined, core24 base.

### Interfaces

| Interface | For | Connect with |
| --- | --- | --- |
| `camera` | camera models | `sudo snap connect impulse-runner:camera` |
| `alsa` | audio models | `sudo snap connect impulse-runner:alsa` |
| `hardware-observe` | CPU temperature and board name | `sudo snap connect impulse-runner:hardware-observe` |

### Tested on

Raspberry Pi 5 (8 GB), Ubuntu Core 24, Logitech C270:

- YOLOv5 COCO (320×320, int8): 13–15 results/s, 45–55 ms per inference, CPU only
- C270 microphone captured at 16 kHz, 2 results/s with 50 % window overlap
- IMU windowing and anomaly scores over HTTP

### Changes since 26.11.0

26.11.0 was only ever released to the edge channel.

- Device stats on one compact row; the detection list takes the free height.
- Redesigned page (layout, typography, sparklines, phone link).
- Removed the `audio-record` plug. Audio goes through ALSA, and the Store requires
  `audio-playback` alongside `audio-record`.

## 26.11.0

Initial build, released to the edge channel only (arm64).

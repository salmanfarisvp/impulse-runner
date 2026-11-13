#!/usr/bin/env python3
"""impulse-runner: run any Edge Impulse .eim model on Ubuntu Core.

The .eim is a standalone executable. We start it with a Unix socket path and
exchange JSON messages with it (the protocol the Edge Impulse Linux SDKs use),
so any model works without per-model code:

  * camera models      -> frames from a V4L2 camera (classification, object
                          detection, FOMO, visual anomaly detection)
  * microphone models  -> audio from ALSA via arecord, sliding window
  * everything else    -> features pushed over HTTP (IMU, environmental,
                          fusion...), or any model via POST /api/classify

Results are served as JSON and as a live web page that Ubuntu Frame can show
full screen.
"""

import json
import math
import os
import platform
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.environ.get("SNAP", HERE)
COMMON = os.environ.get("SNAP_COMMON", os.path.join(os.getcwd(), "data"))
MODEL_PATH = os.path.join(COMMON, "model.eim")
WEB_DIR = os.path.join(SNAP, "www") if "SNAP" in os.environ else os.path.join(HERE, "www")
MAX_UPLOAD = 512 * 1024 * 1024

# model_parameters.sensor values reported by the .eim
SENSORS = {-1: "unknown", 1: "microphone", 2: "accelerometer", 3: "camera",
           4: "positional", 5: "environmental", 6: "fusion"}

EI_TARGET = {"aarch64": "Linux (AARCH64)", "arm64": "Linux (AARCH64)",
             "x86_64": "Linux (x86)", "amd64": "Linux (x86)",
             "armv7l": "Linux (ARMv7)"}.get(platform.machine(), platform.machine())


def log(*args):
    print(*args, flush=True)


def snap_get(key, default=""):
    """Read a snap option; outside a snap, fall back to an env var (PORT, ...)."""
    if "SNAP" in os.environ:
        try:
            out = subprocess.run(["snapctl", "get", key], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            return out or default
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return os.environ.get(key.upper().replace("-", "_"), default)


# --------------------------------------------------------------------------
# The .eim process
# --------------------------------------------------------------------------

class EimRunner:
    """One running .eim. Thread-safe: requests are serialised."""

    def __init__(self, path):
        self._dir = tempfile.mkdtemp(prefix="eim-")
        sock_path = os.path.join(self._dir, "runner.sock")
        # Run a private copy, so model.eim can be overwritten (cp) while we run;
        # Linux refuses to write to an executable that's running ("Text file busy").
        run_dir = os.path.join(COMMON, ".run")
        os.makedirs(run_dir, exist_ok=True)
        self._exe = os.path.join(run_dir, f"model-{os.getpid()}-{time.time_ns()}.eim")
        shutil.copyfile(path, self._exe)
        os.chmod(self._exe, 0o755)
        self.proc = subprocess.Popen([self._exe, sock_path], stdout=subprocess.DEVNULL)
        deadline = time.time() + 60
        while not os.path.exists(sock_path):
            if self.proc.poll() is not None:
                self._cleanup()
                raise RuntimeError(
                    f"model exited with code {self.proc.returncode}. Is it the "
                    f"{EI_TARGET} build? Re-export it from Edge Impulse for {EI_TARGET}.")
            if time.time() > deadline:
                self._cleanup()
                raise RuntimeError("timed out waiting for the model to start")
            time.sleep(0.1)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(sock_path)
        self.sock.settimeout(30)
        self._lock = threading.Lock()
        self._id = 0

        hello = self._send({"hello": 1})
        p = self.params = hello["model_parameters"]
        self.project = hello.get("project", {})
        self.labels = [str(x) for x in p.get("labels", [])]
        self.sensor = SENSORS.get(int(p.get("sensor", -1)), "unknown")
        self.kind = p.get("model_type", "classification")
        self.width = int(p.get("image_input_width", 0) or 0)
        self.height = int(p.get("image_input_height", 0) or 0)
        self.channels = int(p.get("image_channel_count", 3) or 3)
        self.resize_mode = p.get("image_resize_mode", "fit-shortest")
        if self.resize_mode not in ("fit-shortest", "fit-longest", "squash"):
            self.resize_mode = "squash"
        self.frequency = float(p.get("frequency", 0) or 0)
        self.axis_count = int(p.get("axis_count", 1) or 1)
        self.features_count = int(p.get("input_features_count", 0) or 0)
        self.has_anomaly = bool(p.get("has_anomaly", 0))
        self.label_map = coco_label_map(self.labels)
        self.name = (f"{self.project.get('owner', '')} / {self.project.get('name', '')}"
                     .strip(" /") or os.path.basename(path))
        self.version = self.project.get("deploy_version")

    def info(self):
        return {
            "name": self.name, "version": self.version, "project": self.project,
            "sensor": self.sensor, "model_type": self.kind,
            "labels": [self.label_map.get(x, x) for x in self.labels],
            "input_features_count": self.features_count,
            "frequency": self.frequency, "axis_count": self.axis_count,
            "image": ({"width": self.width, "height": self.height,
                       "channels": self.channels, "resize_mode": self.resize_mode}
                      if self.width else None),
            "has_anomaly": self.has_anomaly,
            "parameters": self.params,
        }

    def _send(self, msg):
        with self._lock:
            self._id += 1
            msg["id"] = self._id
            self.sock.sendall(json.dumps(msg).encode())
            buf = b""
            while True:
                chunk = self.sock.recv(1 << 16)
                if not chunk:
                    raise RuntimeError("model closed the connection")
                buf += chunk
                try:
                    resp = json.loads(buf.rstrip(b"\x00").decode())
                    break
                except ValueError:
                    continue  # not complete yet
        if not resp.get("success", False):
            raise RuntimeError(resp.get("error", "model returned an error"))
        return resp

    def classify(self, features):
        """Raw features in, normalised result out (labels mapped)."""
        if self.features_count and len(features) != self.features_count:
            raise ValueError(f"model expects {self.features_count} features, got {len(features)}")
        resp = self._send({"classify": list(features)})
        result = resp.get("result", {})
        name = lambda label: self.label_map.get(label, label)
        if "classification" in result:
            result["classification"] = {name(k): float(v)
                                        for k, v in result["classification"].items()}
        for key in ("bounding_boxes", "visual_anomaly_grid"):
            for b in result.get(key, []) or []:
                b["label"] = name(b.get("label", ""))
        result["timing"] = resp.get("timing", {})
        return result

    def _cleanup(self):
        shutil.rmtree(self._dir, ignore_errors=True)
        try:
            os.remove(self._exe)
        except OSError:
            pass

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass
        self.proc.kill()
        self.proc.wait()
        self._cleanup()


# COCO's 80 classes in training order. Some public Edge Impulse YOLO projects
# ship with placeholder labels ("class 1" ... "class 80"); we map those back.
COCO_LABELS = (
    "person bicycle car motorcycle airplane bus train truck boat traffic_light "
    "fire_hydrant stop_sign parking_meter bench bird cat dog horse sheep cow elephant "
    "bear zebra giraffe backpack umbrella handbag tie suitcase frisbee skis snowboard "
    "sports_ball kite baseball_bat baseball_glove skateboard surfboard tennis_racket "
    "bottle wine_glass cup fork knife spoon bowl banana apple sandwich orange broccoli "
    "carrot hot_dog pizza donut cake chair couch potted_plant bed dining_table toilet tv "
    "laptop mouse remote keyboard cell_phone microwave oven toaster sink refrigerator "
    "book clock vase scissors teddy_bear hair_drier toothbrush").split()
COCO_LABELS = [x.replace("_", " ") for x in COCO_LABELS]


def coco_label_map(labels):
    if len(labels) != 80:
        return {}
    mapping = {}
    for label in labels:
        parts = label.split()
        if len(parts) != 2 or parts[0].lower() != "class" or not parts[1].isdigit():
            return {}
        idx = int(parts[1]) - 1
        if not 0 <= idx < 80:
            return {}
        mapping[label] = COCO_LABELS[idx]
    return mapping


def summarise(result, threshold):
    """Filter boxes by threshold and pick the top prediction."""
    out = dict(result)
    boxes = [b for b in result.get("bounding_boxes", []) or [] if b.get("value", 0) >= threshold]
    out["bounding_boxes"] = boxes
    top = None
    if boxes:
        best = max(boxes, key=lambda b: b["value"])
        top = {"label": best["label"], "value": best["value"]}
    elif result.get("classification"):
        label, value = max(result["classification"].items(), key=lambda kv: kv[1])
        if value >= threshold:
            top = {"label": label, "value": value}
    if result.get("visual_anomaly_max") is not None:
        grid = [b for b in result.get("visual_anomaly_grid", []) or []
                if b.get("value", 0) >= threshold]
        out["visual_anomaly_grid"] = grid
        if grid and top is None:
            top = {"label": "anomaly", "value": result["visual_anomaly_max"]}
    out["top"] = top
    return out


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

class Source:
    """Base for an input loop. Publishes into the shared Hub."""

    mode = "api"

    def __init__(self, hub, runner, settings):
        self.hub, self.runner, self.settings = hub, runner, settings
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def _run(self):
        try:
            self.run()
        except Exception as e:  # keep the service alive; the hub shows the error
            log(f"{self.mode} input stopped: {e}")
            self.hub.update(state="error", message=f"{self.mode} input stopped: {e}")

    def run(self):
        self.hub.update(state="ok", message=None)


class ApiSource(Source):
    """No live input: features arrive via POST /api/classify or /api/data."""

    mode = "api"

    def __init__(self, hub, runner, settings):
        super().__init__(hub, runner, settings)
        n = max(runner.features_count, 1)
        self.buffer = deque(maxlen=n)
        self.pending = 0
        # Classify on every quarter window of new data (whole samples only).
        step = max(n // 4, runner.axis_count)
        self.step = step - step % runner.axis_count or runner.axis_count
        self.lock = threading.Lock()

    def run(self):
        self.hub.update(state="ok", message="Waiting for data on POST /api/data or /api/classify")

    def push(self, values):
        """Append raw values (flat, or rows of axis_count). Classify when a window is full."""
        flat = []
        for v in values:
            if isinstance(v, (list, tuple)):
                flat.extend(float(x) for x in v)
            else:
                flat.append(float(v))
        result = None
        with self.lock:
            for v in flat:
                self.buffer.append(v)
                self.pending += 1
                if len(self.buffer) == self.buffer.maxlen and self.pending >= self.step:
                    self.pending = 0
                    result = self.hub.classify(self.runner, list(self.buffer))
            have = len(self.buffer)
        return {"buffered": have, "window": self.buffer.maxlen, "result": result}


class CameraSource(Source):
    mode = "camera"

    def run(self):
        import cv2
        import numpy as np
        r = self.runner
        cam = self.settings["camera"]
        dev = int(cam) if str(cam).isdigit() else cam
        width, height = self.settings["resolution"]
        cap = None
        fps, last = 0.0, time.time()
        while not self.stop.is_set():
            if cap is None:
                backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
                cap = cv2.VideoCapture(dev, backend)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                if not cap.isOpened():
                    cap = None
                    self.hub.update(state="error", message=(
                        f"No camera at {camera_name(cam)}. Plug one in and run: "
                        "sudo snap connect impulse-runner:camera"))
                    self.stop.wait(2)
                    continue
                log(f"Camera {camera_name(cam)} opened")
            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = None
                self.stop.wait(0.5)
                continue

            feats, region = image_features(frame, r, cv2, np)
            result = self.hub.classify(r, feats)
            if result is None:
                continue
            fh, fw = frame.shape[:2]
            to_frame = region_mapper(region, r)
            for key in ("bounding_boxes", "visual_anomaly_grid"):
                for b in result.get(key, []) or []:
                    b["x"], b["y"], b["width"], b["height"] = to_frame(
                        b["x"], b["y"], b["width"], b["height"])
            result = summarise(result, self.hub.threshold)
            draw(frame, region, result, cv2)

            now = time.time()
            fps = 0.9 * fps + 0.1 / max(now - last, 1e-6)
            last = now
            ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            self.hub.publish(result, fps=round(fps, 1), frame=jpeg.tobytes() if ok else None,
                             frame_size=[fw, fh])
        if cap is not None:
            cap.release()


def camera_name(cam):
    return f"/dev/video{cam}" if str(cam).isdigit() else str(cam)


def image_features(frame, r, cv2, np):
    """Resize like Edge Impulse Studio does; return (features, region).

    region = (x, y, w, h, pad_x, pad_y, scale) describing where the model input
    sits in the frame, so boxes can be mapped back.
    """
    fh, fw = frame.shape[:2]
    W, H = r.width, r.height
    if r.resize_mode == "fit-shortest":       # centre crop
        target = W / H
        cw, ch = (int(fh * target), fh) if fw / fh > target else (fw, int(fw / target))
        ox, oy = (fw - cw) // 2, (fh - ch) // 2
        img = cv2.resize(frame[oy:oy + ch, ox:ox + cw], (W, H), interpolation=cv2.INTER_AREA)
        region = ("crop", ox, oy, cw / W, ch / H)
    elif r.resize_mode == "fit-longest":      # letterbox
        s = min(W / fw, H / fh)
        nw, nh = int(fw * s), int(fh * s)
        img = np.zeros((H, W, 3), dtype=frame.dtype)
        px, py = (W - nw) // 2, (H - nh) // 2
        img[py:py + nh, px:px + nw] = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        region = ("pad", px, py, s, s)
    else:                                     # squash
        img = cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA)
        region = ("squash", 0, 0, fw / W, fh / H)

    if r.channels == 1:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.uint32)
        feats = g * 0x010101
    else:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.uint32)
        feats = (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]
    return feats.flatten().tolist(), region


def region_mapper(region, r):
    kind, a, b, sx, sy = region
    if kind == "pad":   # model px -> frame px: remove padding, then unscale
        return lambda x, y, w, h: (int((x - a) / sx), int((y - b) / sy), int(w / sx), int(h / sy))
    return lambda x, y, w, h: (int(a + x * sx), int(b + y * sy), int(w * sx), int(h * sy))


def draw(frame, region, result, cv2):
    kind, a, b, sx, sy = region
    if kind == "crop":  # show the part of the frame the model actually sees
        fh, fw = frame.shape[:2]
        cv2.rectangle(frame, (a, b), (fw - a - 1, fh - b - 1), (90, 90, 90), 1)
    for key, color in (("visual_anomaly_grid", (60, 60, 230)), ("bounding_boxes", (120, 220, 60))):
        for box in result.get(key, []) or []:
            x, y, w, h = box["x"], box["y"], box["width"], box["height"]
            if result.get("model_type") == "constrained_object_detection" or (w <= 12 and h <= 12):
                cv2.circle(frame, (x + w // 2, y + h // 2), 8, color, 2)
            else:
                cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
            cv2.putText(frame, f"{box['label']} {box['value']:.2f}", (x, max(14, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)


class MicrophoneSource(Source):
    """ALSA capture via arecord, int16 mono at the model's frequency."""

    mode = "microphone"

    def run(self):
        r = self.runner
        rate = int(r.frequency)
        window = r.features_count
        step = max(int(window * (1 - self.settings["overlap"])), 1)
        device = self.settings["audio_device"]
        chunk = max(rate // 20, 256)  # ~50 ms
        while not self.stop.is_set():
            cmd = ["arecord", "-q", "-D", device, "-f", "S16_LE", "-c", "1",
                   "-r", str(rate), "-t", "raw"]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except FileNotFoundError:
                raise RuntimeError("arecord not found")
            log(f"Microphone {device} opened at {rate} Hz (window {window}, step {step})")
            samples = deque(maxlen=window)
            fresh = 0
            fps, last = 0.0, time.time()
            while not self.stop.is_set():
                data = proc.stdout.read(chunk * 2)
                if not data:
                    break
                vals = struct.unpack(f"<{len(data) // 2}h", data[:len(data) // 2 * 2])
                samples.extend(vals)
                fresh += len(vals)
                level = math.sqrt(sum(v * v for v in vals) / max(len(vals), 1)) / 32768
                if len(samples) < window or fresh < step:
                    self.hub.update(level=round(level, 3))
                    continue
                fresh = 0
                result = self.hub.classify(r, list(samples))
                if result is None:
                    continue
                now = time.time()
                fps = 0.8 * fps + 0.2 / max(now - last, 1e-6)
                last = now
                self.hub.publish(summarise(result, self.hub.threshold),
                                 fps=round(fps, 1), level=round(level, 3))
            proc.kill()
            err = proc.stderr.read().decode(errors="replace").strip()
            proc.wait()
            if self.stop.is_set():
                break
            self.hub.update(state="error", message=(
                f"Microphone {device} failed: {err or 'no data'}. Plug one in and run: "
                "sudo snap connect impulse-runner:audio-record (or :alsa)"))
            log(f"arecord failed: {err}")
            self.stop.wait(3)


SOURCES = {"camera": CameraSource, "microphone": MicrophoneSource, "api": ApiSource}


def pick_mode(runner, wanted):
    if wanted in SOURCES:
        return wanted
    if runner.width and runner.height:
        return "camera"
    if runner.sensor == "microphone" and runner.frequency and runner.axis_count == 1:
        return "microphone"
    return "api"


# --------------------------------------------------------------------------
# Hub: current model, input, latest result
# --------------------------------------------------------------------------

class Hub:
    def __init__(self, settings):
        self.settings = settings
        self.threshold = settings["threshold"]
        self.cond = threading.Condition()
        self.runner = None
        self.source = None
        self.model_key = None
        self.frame = None
        self.seq = 0
        self.status = {"state": "starting", "message": "Starting..."}
        self.reload_lock = threading.Lock()

    # -- status --------------------------------------------------------------
    def update(self, **kw):
        with self.cond:
            self.status.update(kw)
            self.cond.notify_all()

    def publish(self, result, frame=None, **kw):
        with self.cond:
            if frame is not None:
                self.frame = frame
                self.seq += 1
            self.status.update(state="ok", message=None, result=result,
                               updated=time.time(), **kw)
            self.cond.notify_all()

    def snapshot(self):
        with self.cond:
            s = dict(self.status)
        s["threshold"] = self.threshold
        s["model_path"] = MODEL_PATH
        s["target"] = EI_TARGET
        s["upload_enabled"] = bool(self.settings["upload_token"])
        return s

    def classify(self, runner, features):
        """Run the model; on a broken model process, schedule a reload."""
        t0 = time.time()
        try:
            result = runner.classify(features)
        except ValueError:
            raise
        except Exception as e:
            log(f"Inference failed: {e}")
            self.update(state="error", message=f"Inference failed: {e}")
            self.model_key = None  # watcher reloads
            time.sleep(0.5)
            return None
        result["latency_ms"] = round((time.time() - t0) * 1000, 1)
        result["model_type"] = runner.kind
        return result

    # -- model lifecycle -----------------------------------------------------
    @staticmethod
    def current_key():
        try:
            return MODEL_PATH, os.path.getmtime(MODEL_PATH), os.path.getsize(MODEL_PATH)
        except OSError:
            return None

    def watch(self):
        while True:
            key = self.current_key()
            if key != self.model_key:
                self.reload(key)
            time.sleep(2)

    def reload(self, key):
        with self.reload_lock:
            if self.source:
                self.source.close()
                self.source = None
            if self.runner:
                self.runner.close()
                self.runner = None
            self.model_key = key
            with self.cond:
                self.frame = None
                self.status = {"state": "starting", "message": "Loading model..."}
            if key is None:
                self.update(state="nomodel", model=None, mode=None, message=(
                    f"No model yet. Upload a {EI_TARGET} .eim from this page, "
                    f"copy one to {MODEL_PATH}, or snap set impulse-runner model-url=..."))
                return
            try:
                runner = EimRunner(MODEL_PATH)
            except Exception as e:
                log(f"Model failed to load: {e}")
                self.update(state="error", model=None, mode=None, message=f"Model failed to load: {e}")
                return
            mode = pick_mode(runner, self.settings["input"])
            info = runner.info()
            log(f"Loaded model: {runner.name} v{runner.version} ({runner.kind}, sensor "
                f"{runner.sensor}, {runner.features_count} features, {len(runner.labels)} labels"
                f") -> input: {mode}")
            if mode == "camera" and not runner.width:
                mode = "api"
                log("input=camera, but this model has no image input; using api")
            if mode == "microphone" and not runner.frequency:
                mode = "api"
                log("input=microphone, but this model has no sampling frequency; using api")
            self.runner = runner
            self.update(model=info, mode=mode, result=None)
            self.source = SOURCES[mode](self, runner, self.settings).start()

    def save_model(self, stream, length):
        os.makedirs(COMMON, exist_ok=True)
        tmp = MODEL_PATH + ".upload"
        with open(tmp, "wb") as f:
            remaining = length
            head = b""
            while remaining:
                chunk = stream.read(min(remaining, 1 << 20))
                if not chunk:
                    raise ValueError("upload interrupted")
                if not head:
                    head = chunk[:4]
                f.write(chunk)
                remaining -= len(chunk)
        if head != b"\x7fELF":
            os.remove(tmp)
            raise ValueError("not a Linux executable; export the model as "
                             f"'{EI_TARGET}' (.eim) in Edge Impulse Studio")
        os.chmod(tmp, 0o755)
        os.replace(tmp, MODEL_PATH)
        try:
            os.remove(MODEL_PATH + ".url")  # an upload overrides model-url
        except OSError:
            pass
        self.reload(self.current_key())


def fetch_model_url():
    url = snap_get("model-url", "")
    marker = MODEL_PATH + ".url"
    current = open(marker).read().strip() if os.path.exists(marker) else ""
    if not url or (url == current and os.path.exists(MODEL_PATH)):
        return
    log(f"Downloading model from {url}")
    os.makedirs(COMMON, exist_ok=True)
    tmp = MODEL_PATH + ".part"
    urllib.request.urlretrieve(url, tmp)
    with open(tmp, "rb") as f:
        if f.read(4) != b"\x7fELF":
            os.remove(tmp)
            raise ValueError("downloaded file is not a .eim (Linux executable)")
    os.chmod(tmp, 0o755)
    os.replace(tmp, MODEL_PATH)
    with open(marker, "w") as f:
        f.write(url)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def parse_features(body, ctype):
    """JSON {"features": [...]} / [...] or plain text "1, 2, 3"."""
    text = body.decode().strip()
    if "json" in ctype or text.startswith(("{", "[")):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("features", data.get("values"))
        if not isinstance(data, list):
            raise ValueError('send {"features": [...]}')
        return data
    return [float(x) for x in text.replace("\n", ",").split(",") if x.strip()]


def flatten(values):
    out = []
    for v in values:
        out.extend(float(x) for x in v) if isinstance(v, (list, tuple)) else out.append(float(v))
    return out


def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # keep `snap logs` readable

        def _send(self, code, body, ctype="application/json", headers=None):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _query(self):
            path, _, q = self.path.partition("?")
            return path, dict(p.split("=", 1) for p in q.split("&") if "=" in p)

        def _body(self, limit=64 * 1024 * 1024):
            n = int(self.headers.get("Content-Length", 0))
            if n > limit:
                raise ValueError("request too large")
            return self.rfile.read(n)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Upload-Token")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path, q = self._query()
            if path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/status":
                self._send(200, hub.snapshot())
            elif path == "/api/model":
                r = hub.runner
                self._send(200, r.info()) if r else self._send(404, {"error": "no model loaded"})
            elif path == "/frame.jpg":
                try:
                    after = int(q.get("after", -1))
                except ValueError:
                    after = -1
                with hub.cond:
                    hub.cond.wait_for(lambda: hub.frame is not None and hub.seq != after, timeout=2)
                    frame, seq = hub.frame, hub.seq
                if frame is None:
                    self._send(503, b"no frame", "text/plain")
                else:
                    self._send(200, frame, "image/jpeg", {"X-Frame-Seq": str(seq)})
            elif path == "/stream.mjpg":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                seen = -1
                try:
                    while True:
                        with hub.cond:
                            hub.cond.wait_for(lambda: hub.frame is not None and hub.seq != seen, timeout=5)
                            frame, seen = hub.frame, hub.seq
                        if frame is None:
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path, q = self._query()
            try:
                if path == "/api/classify":
                    r = hub.runner
                    if not r:
                        return self._send(409, {"error": "no model loaded"})
                    feats = flatten(parse_features(self._body(), self.headers.get("Content-Type", "")))
                    result = hub.classify(r, feats)
                    if result is None:
                        return self._send(500, {"error": "inference failed"})
                    result = summarise(result, hub.threshold)
                    if hub.status.get("mode") == "api":
                        hub.publish(result)
                    return self._send(200, result)
                if path == "/api/data":
                    src = hub.source
                    if not isinstance(src, ApiSource):
                        return self._send(409, {"error": "input is not 'api' "
                                                "(snap set impulse-runner input=api)"})
                    values = parse_features(self._body(), self.headers.get("Content-Type", ""))
                    out = src.push(values)
                    if out["result"] is not None:
                        out["result"] = summarise(out["result"], hub.threshold)
                        hub.publish(out["result"])
                    return self._send(200, out)
                if path == "/api/model":
                    token = hub.settings["upload_token"]
                    given = self.headers.get("X-Upload-Token") or q.get("token", "")
                    if not token:
                        return self._send(403, {"error": "uploads are disabled "
                                                "(snap set impulse-runner upload-token=...)"})
                    if not secrets.compare_digest(given, token):
                        return self._send(401, {"error": "wrong upload token. Get it with: "
                                                "sudo snap get impulse-runner upload-token"})
                    n = int(self.headers.get("Content-Length", 0))
                    if not 0 < n <= MAX_UPLOAD:
                        return self._send(413, {"error": "missing or too large"})
                    hub.save_model(self.rfile, n)
                    if hub.runner is None:
                        return self._send(422, {"error": hub.status.get("message")})
                    return self._send(200, {"ok": True, "model": hub.runner.info(),
                                            "mode": hub.status.get("mode")})
                return self._send(404, {"error": "not found"})
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})

    return Handler


def load_settings():
    res = snap_get("camera-resolution", "640x480").lower().split("x")
    try:
        resolution = (int(res[0]), int(res[1]))
    except (ValueError, IndexError):
        resolution = (640, 480)
    return {
        "port": int(snap_get("port", "8000")),
        "input": snap_get("input", "auto"),
        "camera": snap_get("camera", "0"),
        "resolution": resolution,
        "audio_device": snap_get("audio-device", "default"),
        "overlap": min(max(float(snap_get("audio-overlap", "0.5")), 0.0), 0.95),
        "threshold": float(snap_get("threshold", "0.5")),
        "upload_token": snap_get("upload-token", ""),
    }


def main():
    settings = load_settings()
    try:
        fetch_model_url()
    except Exception as e:
        log(f"Model download failed: {e}")

    shutil.rmtree(os.path.join(COMMON, ".run"), ignore_errors=True)  # stale model copies
    hub = Hub(settings)
    threading.Thread(target=hub.watch, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", settings["port"]), make_handler(hub))
    server.daemon_threads = True
    log(f"impulse-runner on http://0.0.0.0:{settings['port']}  (input {settings['input']}, "
        f"threshold {settings['threshold']}, target {EI_TARGET})")
    server.serve_forever()


def cli_info():
    """impulse-runner.info: print what the installed model expects."""
    if not os.path.exists(MODEL_PATH):
        sys.exit(f"No model at {MODEL_PATH}")
    r = EimRunner(MODEL_PATH)
    try:
        info = r.info()
        info.pop("parameters")
        info["suggested_input"] = pick_mode(r, "auto")
        print(json.dumps(info, indent=2))
    finally:
        r.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "info":
        cli_info()
    else:
        main()

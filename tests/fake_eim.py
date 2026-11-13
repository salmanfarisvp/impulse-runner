#!/usr/bin/env python3
"""A stand-in .eim for tests: speaks the same socket protocol.

FAKE_KIND (env, or the script's file name containing it) picks the model:
  camera  - 96x96 object detection (FOMO-style centroid boxes)
  audio   - 16 kHz, 1 s window keyword spotting ("yes", "no", "noise")
  imu     - 3-axis accelerometer @ 62.5 Hz, 2 s window, + anomaly score
"""
import json, os, socket, sys

kind = os.environ.get("FAKE_KIND") or next(
    (k for k in ("camera", "audio", "imu") if k in os.path.basename(sys.argv[0])), "imu")

PARAMS = {
    "camera": dict(sensor=3, model_type="constrained_object_detection", image_input_width=96,
                   image_input_height=96, image_channel_count=1, image_resize_mode="squash",
                   frequency=0, axis_count=1, input_features_count=96 * 96, labels=["cup", "pen"]),
    "audio": dict(sensor=1, model_type="classification", image_input_width=0, image_input_height=0,
                  frequency=16000, axis_count=1, input_features_count=16000,
                  labels=["no", "noise", "yes"]),
    "imu": dict(sensor=2, model_type="classification", image_input_width=0, image_input_height=0,
                frequency=62.5, axis_count=3, input_features_count=375, has_anomaly=1,
                labels=["idle", "updown", "wave"]),
}[kind]

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sys.argv[1]); srv.listen(1)
conn, _ = srv.accept()
buf = b""
while True:
    data = conn.recv(1 << 20)
    if not data:
        break
    buf += data
    try:
        msg = json.loads(buf); buf = b""
    except ValueError:
        continue
    if "hello" in msg:
        resp = {"success": True, "id": msg["id"], "model_parameters": PARAMS,
                "project": {"owner": "test", "name": f"fake {kind}", "deploy_version": 1}}
    else:
        f = msg["classify"]
        if len(f) != PARAMS["input_features_count"]:
            resp = {"success": False, "id": msg["id"], "error": "wrong feature count"}
        elif kind == "camera":
            resp = {"success": True, "id": msg["id"], "result": {"bounding_boxes": [
                {"label": "cup", "value": 0.9, "x": 40, "y": 40, "width": 8, "height": 8},
                {"label": "pen", "value": 0.2, "x": 8, "y": 8, "width": 8, "height": 8}]}}
        elif kind == "audio":
            loud = max(abs(int(x)) for x in f) > 3000
            resp = {"success": True, "id": msg["id"], "result": {"classification": {
                "yes": 0.9 if loud else 0.05, "no": 0.03, "noise": 0.07 if loud else 0.92}}}
        else:
            moving = max(f) - min(f) > 5
            resp = {"success": True, "id": msg["id"], "result": {
                "classification": {"idle": 0.1 if moving else 0.95, "wave": 0.85 if moving else 0.03,
                                   "updown": 0.05 if moving else 0.02},
                "anomaly": -0.3}}
    resp["timing"] = {"dsp": 1, "classification": 2}
    conn.sendall(json.dumps(resp).encode() + b"\x00")

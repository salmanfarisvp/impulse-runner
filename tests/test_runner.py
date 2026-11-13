#!/usr/bin/env python3
"""End-to-end tests against the fake .eim. Run: python3 tests/test_runner.py"""
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
FAKE = os.path.join(ROOT, "tests", "fake_eim.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self, kind):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "data"))
        shutil.copy(FAKE, os.path.join(self.dir, "data", "model.eim"))
        self.port = free_port()
        env = dict(os.environ, PORT=str(self.port), UPLOAD_TOKEN="secret", FAKE_KIND=kind,
                   INPUT="api" if kind == "imu" else "auto")
        env.pop("SNAP", None)
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "impulse_runner.py")],
                                     cwd=self.dir, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                if self.get("/api/status").get("model"):
                    return
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError("server did not start")

    def get(self, path):
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5))

    def post(self, path, body, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data,
                                     headers or {"Content-Type": "application/json"})
        try:
            return 200, json.load(urllib.request.urlopen(req, timeout=10))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def stop(self):
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class ImuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Server("imu")

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def test_model_info(self):
        m = self.s.get("/api/model")
        self.assertEqual(m["sensor"], "accelerometer")
        self.assertEqual(m["axis_count"], 3)
        self.assertEqual(self.s.get("/api/status")["mode"], "api")

    def test_classify_window(self):
        code, r = self.s.post("/api/classify", {"features": [0.0] * 375})
        self.assertEqual(code, 200)
        self.assertEqual(r["top"]["label"], "idle")
        self.assertIn("anomaly", r)

    def test_classify_text_body(self):
        code, r = self.s.post("/api/classify", ",".join(["1"] * 375).encode(),
                              {"Content-Type": "text/plain"})
        self.assertEqual(code, 200)

    def test_wrong_length(self):
        code, r = self.s.post("/api/classify", {"features": [1, 2, 3]})
        self.assertEqual(code, 400)
        self.assertIn("375", r["error"])

    def test_streaming_data(self):
        results = []
        for i in range(6):
            rows = [[10 * math.sin(j / 3), 0, 9.8] for j in range(i * 40, i * 40 + 40)]
            code, r = self.s.post("/api/data", {"values": rows})
            self.assertEqual(code, 200)
            results.append(r["result"])
        self.assertIsNone(results[0])  # window not full yet
        self.assertEqual(results[-1]["top"]["label"], "wave")

    def test_upload_needs_token(self):
        code, _ = self.s.post("/api/model", b"\x7fELF....", {"X-Upload-Token": "nope"})
        self.assertEqual(code, 401)

    def test_upload_rejects_non_elf(self):
        code, r = self.s.post("/api/model", b"#!/bin/sh\n", {"X-Upload-Token": "secret"})
        self.assertEqual(code, 400)
        self.assertIn(".eim", r["error"])


class MappingTests(unittest.TestCase):
    def test_coco_labels(self):
        import impulse_runner as ir
        m = ir.coco_label_map([f"class {i}" for i in range(1, 81)])
        self.assertEqual(m["class 1"], "person")
        self.assertEqual(m["class 57"], "chair")
        self.assertEqual(ir.coco_label_map(["cat", "dog"]), {})

    def test_summarise_threshold(self):
        import impulse_runner as ir
        r = ir.summarise({"bounding_boxes": [{"label": "a", "value": 0.9},
                                             {"label": "b", "value": 0.3}]}, 0.5)
        self.assertEqual([b["label"] for b in r["bounding_boxes"]], ["a"])
        self.assertEqual(r["top"]["label"], "a")
        r = ir.summarise({"classification": {"x": 0.4, "y": 0.6}}, 0.5)
        self.assertEqual(r["top"], {"label": "y", "value": 0.6})

    def test_box_mapping(self):
        try:
            import cv2, numpy as np
        except ImportError:
            self.skipTest("opencv not installed")
        import impulse_runner as ir

        class R:  # model stand-in
            width = height = 100
            channels = 3

        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        for mode, expect in (("fit-shortest", (80, 0, 480, 480)),   # centre crop
                             ("squash", (0, 0, 640, 480)),
                             ("fit-longest", (0, 0, 640, 480))):     # letterbox
            R.resize_mode = mode
            feats, region = ir.image_features(frame, R, cv2, np)
            self.assertEqual(len(feats), 100 * 100)
            if mode == "fit-longest":  # whole model input minus padding = whole frame
                _, px, py, _, _ = region
                got = ir.region_mapper(region, R)(px, py, 100 - 2 * px, 100 - 2 * py)
            else:
                got = ir.region_mapper(region, R)(0, 0, 100, 100)
            for g, e in zip(got, expect):
                self.assertLessEqual(abs(g - e), 7, f"{mode}: {got} vs {expect}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Message building, device signing and publishing sinks for the Reality Emulator.

Signing
    device_key(master, source_id) = HMAC-SHA256(master, source_id)
    sig = HMAC-SHA256(device_key, canonical JSON of the envelope without "sig")
    canonical JSON: sort_keys, separators (",", ":"), allow_nan (NaN / Infinity survive, so layer 1
    can reject them rather than the transport)
Sinks
    MemorySink     keeps messages in a list (tests, in-process pipelines)
    JsonlSink      one JSON message per line (optionally .gz): the benchmark file format
    HttpSink       POST batches to {base}/api/v1/ingest/{channel}; headers
                   X-AEGIS-Publisher, X-AEGIS-Timestamp, X-AEGIS-Nonce,
                   X-AEGIS-Signature = HMAC-SHA256(publisher_key, f"{ts}\\n{nonce}\\n{body}")
                   body = {"messages": [...]}; 202 expected; failures are counted, never raised mid-run
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import io
import itertools
import json
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from collections.abc import Iterable

from sim.reality.schemas import CHANNEL

DEFAULT_MASTER = b"aegis-demo-master-key-change-me"


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=True)


def device_key(master: bytes, source_id: str) -> bytes:
    return hmac.new(master, source_id.encode(), hashlib.sha256).digest()


class Signer:
    def __init__(self, master: bytes = DEFAULT_MASTER):
        self.master = master
        self._keys: dict[str, bytes] = {}

    def key(self, source_id: str) -> bytes:
        k = self._keys.get(source_id)
        if k is None:
            k = self._keys[source_id] = device_key(self.master, source_id)
        return k

    def sign(self, msg: dict) -> dict:
        body = {k: v for k, v in msg.items() if k != "sig"}
        msg["sig"] = hmac.new(self.key(msg["source_id"]), canonical(body).encode(), hashlib.sha256).hexdigest()
        return msg

    def verify(self, msg: dict) -> bool:
        return self.verify_with(msg, self.key(msg["source_id"]))

    @staticmethod
    def verify_with(msg: dict, key: bytes) -> bool:
        body = {k: v for k, v in msg.items() if k != "sig"}
        want = hmac.new(key, canonical(body).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, str(msg.get("sig", "")))


class MessageFactory:
    """Deterministic ids and per-source sequence numbers."""
    def __init__(self, seed: int, signer: Signer, salt: str = ""):
        self.seed, self.signer, self.salt = seed, signer, salt
        self._n = itertools.count()
        self._seq: defaultdict[str, int] = defaultdict(int)

    def make(self, kind: str, source_id: str, ts: str, payload: dict) -> dict:
        n = next(self._n)
        seq = self._seq[source_id]
        self._seq[source_id] = seq + 1
        msg_id = hashlib.blake2s(f"{self.seed}:{self.salt}:{n}".encode() if self.salt else f"{self.seed}:{n}".encode(),
                                 digest_size=10).hexdigest()
        return self.signer.sign({"msg_id": msg_id, "source_id": source_id, "kind": kind, "ts": ts, "seq": seq,
                                 "payload": payload})


# ------------------------------------------------------------------------------------------------ sinks
class Sink:
    def publish(self, msgs: Iterable[dict]) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.flush()


class MemorySink(Sink):
    def __init__(self):
        self.messages: list[dict] = []

    def publish(self, msgs):
        self.messages.extend(msgs)


class JsonlSink(Sink):
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".gz":  # mtime=0 and no name in the header: byte-identical output for identical input
            self._raw = path.open("wb")
            self.f = io.TextIOWrapper(gzip.GzipFile(filename="", mode="wb", fileobj=self._raw, compresslevel=6, mtime=0),
                                      encoding="utf-8")
        else:
            self._raw = None
            self.f = path.open("w")
        self.count = 0

    def publish(self, msgs):
        for m in msgs:
            self.f.write(canonical(m) + "\n")
            self.count += 1

    def close(self):
        self.f.close()
        if self._raw is not None:
            self._raw.close()


class HttpSink(Sink):
    def __init__(self, base_url: str, publisher_id: str = "reality-emulator", publisher_key: bytes = DEFAULT_MASTER,
                 batch_size: int = 500, timeout_s: float = 5.0, flush_every_s: float = 0.5):
        self.base = base_url.rstrip("/")
        self.publisher_id, self.key = publisher_id, publisher_key
        self.batch_size, self.timeout_s, self.flush_every_s = batch_size, timeout_s, flush_every_s
        self.buf: defaultdict[str, list[dict]] = defaultdict(list)
        self.sent = self.failed = self.batches = 0
        self.status_counts: defaultdict[int, int] = defaultdict(int)
        self._last = time.monotonic()
        self._nonce = itertools.count()
        self._lock = threading.Lock()

    def headers(self, body: bytes) -> dict:
        ts = f"{time.time():.3f}"
        nonce = hashlib.blake2s(f"{self.publisher_id}:{next(self._nonce)}:{ts}".encode(), digest_size=12).hexdigest()
        sig = hmac.new(self.key, f"{ts}\n{nonce}\n".encode() + body, hashlib.sha256).hexdigest()
        return {"Content-Type": "application/json", "X-AEGIS-Publisher": self.publisher_id, "X-AEGIS-Timestamp": ts,
                "X-AEGIS-Nonce": nonce, "X-AEGIS-Signature": sig}

    def publish(self, msgs):
        with self._lock:
            for m in msgs:
                ch = CHANNEL.get(m.get("kind"), "telemetry")
                self.buf[ch].append(m)
                if len(self.buf[ch]) >= self.batch_size:
                    self._post(ch)
            if time.monotonic() - self._last >= self.flush_every_s:
                self._flush_locked()

    def _post(self, ch: str) -> None:
        batch, self.buf[ch] = self.buf[ch], []
        if not batch:
            return
        if not self.base.startswith(("http://", "https://")):
            raise ValueError("the ingest URL must be http(s)")
        body = json.dumps({"messages": batch}, separators=(",", ":"), allow_nan=True).encode()
        req = urllib.request.Request(f"{self.base}/api/v1/ingest/{ch}", data=body, method="POST", headers=self.headers(body))  # noqa: S310
        self.batches += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:  # noqa: S310 - scheme checked above
                self.status_counts[r.status] += 1
                self.sent += len(batch)
        except urllib.error.HTTPError as e:
            self.status_counts[e.code] += 1
            self.failed += len(batch)
        except (urllib.error.URLError, OSError):
            self.status_counts[0] += 1
            self.failed += len(batch)

    def _flush_locked(self):
        for ch in list(self.buf):
            self._post(ch)
        self._last = time.monotonic()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def stats(self) -> dict:
        return {"sent": self.sent, "failed": self.failed, "batches": self.batches, "status": dict(self.status_counts)}


class FanoutSink(Sink):
    def __init__(self, *sinks: Sink):
        self.sinks = sinks

    def publish(self, msgs):
        msgs = list(msgs)
        for s in self.sinks:
            s.publish(msgs)

    def flush(self):
        for s in self.sinks:
            s.flush()

    def close(self):
        for s in self.sinks:
            s.close()

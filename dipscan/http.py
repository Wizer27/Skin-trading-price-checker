"""Generic polite HTTP client: curl or urllib, one request at a time, retries on 429/5xx."""

from __future__ import annotations

import gzip
import json
import random
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

RETRY_STATUS = (408, 429, 500, 502, 503, 504)


class FetchError(RuntimeError):
    """The endpoint kept failing or answered with something unusable."""


class NotFound(FetchError):
    """The endpoint has no such resource — a renamed or delisted ticker, usually."""


class Transport:
    name = "transport"

    def get(self, url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
        raise NotImplementedError


class UrllibTransport(Transport):
    name = "urllib"

    def get(self, url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return response.status, body.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")


class CurlTransport(Transport):
    name = "curl"

    def __init__(self, binary: str = "curl") -> None:
        self.binary = binary

    @staticmethod
    def available() -> bool:
        return shutil.which("curl") is not None

    def get(self, url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
        command = [self.binary, "-sS", "--compressed", "--max-time", str(int(timeout)), "-w", "\n%{http_code}"]
        for key, value in headers.items():
            if key.lower() == "accept-encoding":
                continue  # --compressed negotiates this
            command += ["-H", "%s: %s" % (key, value)]
        command.append(url)
        finished = subprocess.run(command, capture_output=True, timeout=timeout + 10)
        if finished.returncode != 0:
            raise OSError(finished.stderr.decode("utf-8", "replace").strip() or "curl failed")
        body, _, status = finished.stdout.decode("utf-8", "replace").rpartition("\n")
        try:
            return int(status.strip()), body
        except ValueError:
            raise OSError("unexpected curl output")


def make_transport(kind: str = "auto") -> Transport:
    if kind == "urllib":
        return UrllibTransport()
    if kind == "curl":
        return CurlTransport()
    return CurlTransport() if CurlTransport.available() else UrllibTransport()


class Client:
    """Keeps `delay` seconds between requests and retries the statuses worth retrying."""

    def __init__(
        self,
        delay: float = 1.0,
        timeout: float = 25.0,
        retries: int = 3,
        transport: str = "auto",
        headers: Optional[Dict[str, str]] = None,
        sleep=time.sleep,
    ) -> None:
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.transport = make_transport(transport)
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
        if headers:
            self.headers.update(headers)
        self._sleep = sleep
        self._last = 0.0
        self.requests = 0

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)

    def get_text(self, url: str, params: Optional[Dict[str, object]] = None) -> str:
        if params:
            url = "%s?%s" % (url, urllib.parse.urlencode(params))
        last_error: Optional[str] = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                self.requests += 1
                status, body = self.transport.get(url, self.headers, self.timeout)
                self._last = time.monotonic()
                if status == 200:
                    return body
                if status in RETRY_STATUS:
                    last_error = "HTTP %d" % status
                elif status == 404:
                    raise NotFound("HTTP 404 for %s" % url)
                else:
                    raise FetchError("HTTP %d for %s" % (status, url))
            except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
                self._last = time.monotonic()
                last_error = str(exc) or exc.__class__.__name__
            if attempt < self.retries:
                self._sleep(min(30.0, max(self.delay, 1.0) * (2 ** attempt)) + random.uniform(0, 0.5))
        raise FetchError("giving up on %s: %s" % (url, last_error))

    def get_json(self, url: str, params: Optional[Dict[str, object]] = None):
        body = self.get_text(url, params)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise FetchError("%s returned non-JSON: %s" % (url, body[:120])) from exc

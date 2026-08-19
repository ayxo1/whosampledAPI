import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

from wsmpld.models import SamplesResponse


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def _uvicorn_server(port: int) -> Iterator[subprocess.Popen[str]]:
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "wsmpld.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=1)
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        else:
            raise AssertionError("Uvicorn did not become ready within 10 seconds")
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.live
def test_two_live_kanye_west_requests_reuse_browser_clearance() -> None:
    port = _free_loopback_port()
    responses: list[httpx.Response] = []
    request_error: httpx.HTTPError | None = None

    with _uvicorn_server(port) as process:
        try:
            responses = [
                httpx.get(
                    f"http://127.0.0.1:{port}/artists/Kanye-West/samples",
                    timeout=125,
                )
                for _ in range(2)
            ]
        except httpx.HTTPError as error:
            request_error = error

    assert process.stdout is not None
    logs = process.stdout.read()
    assert request_error is None, [repr(request_error), logs]
    if [response.status_code for response in responses] != [200, 200]:
        print(logs)
    assert [response.status_code for response in responses] == [200, 200], [
        *[response.text for response in responses],
        logs,
    ]
    parsed = [SamplesResponse.model_validate(response.json()) for response in responses]
    assert all(result.artist.requested_slug == "Kanye-West" for result in parsed)
    assert all(result.artist.name == "Kanye West" for result in parsed)
    assert all(result.items for result in parsed)

    assert logs.count("visible unattended Camoufox clearance acquisition started") == 1
    assert logs.count("reusing unexpired clearance session") == 1
    assert logs.count("browserless Samples fetch started") == 2
    assert logs.count("Samples data fetch started transport=curl_cffi") == 2
    assert logs.count("Samples data fetch started") == 2

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


def _request_running_api(
    paths: list[str],
) -> tuple[list[httpx.Response], httpx.HTTPError | None, str]:
    port = _free_loopback_port()
    responses: list[httpx.Response] = []
    request_error: httpx.HTTPError | None = None

    with _uvicorn_server(port) as process:
        try:
            responses = [
                httpx.get(f"http://127.0.0.1:{port}{path}", timeout=125) for path in paths
            ]
        except httpx.HTTPError as error:
            request_error = error

    assert process.stdout is not None
    return responses, request_error, process.stdout.read()


def _traverse_running_api(
    artist_slug: str,
    *,
    max_requests: int,
) -> tuple[list[httpx.Response], httpx.HTTPError | None, str]:
    port = _free_loopback_port()
    responses: list[httpx.Response] = []
    request_error: httpx.HTTPError | None = None
    cursor: str | None = None

    with _uvicorn_server(port) as process:
        try:
            while len(responses) < max_requests:
                params = {"limit": "1" if not responses else "max"}
                if cursor is not None:
                    params["cursor"] = cursor
                response = httpx.get(
                    f"http://127.0.0.1:{port}/artists/{artist_slug}/samples",
                    params=params,
                    timeout=125,
                )
                responses.append(response)
                if response.status_code != 200:
                    break
                cursor = SamplesResponse.model_validate(
                    response.json()
                ).pagination.next_cursor
                if cursor is None:
                    break
        except httpx.HTTPError as error:
            request_error = error

    assert process.stdout is not None
    return responses, request_error, process.stdout.read()


@pytest.mark.live
def test_two_live_kanye_west_requests_reuse_the_parsed_page() -> None:
    responses, request_error, logs = _request_running_api(
        ["/artists/Kanye-West/samples"] * 2
    )
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
    assert logs.count("Samples page cache hit") == 1
    assert logs.count("browserless Samples fetch started") == 1
    assert logs.count("Samples data fetch started transport=curl_cffi") == 1
    assert logs.count("Samples data fetch started") == 1


@pytest.mark.live
def test_live_2pac_samples_supports_alternate_artist_credit_markup() -> None:
    responses, request_error, logs = _request_running_api(
        ["/artists/2Pac/samples?limit=max"]
    )
    assert request_error is None, [repr(request_error), logs]
    assert len(responses) == 1
    response = responses[0]
    if response.status_code != 200:
        print(logs)
    assert response.status_code == 200, [response.text, logs]

    parsed = SamplesResponse.model_validate(response.json())
    assert parsed.artist.requested_slug == "2Pac"
    assert parsed.artist.name == "2Pac"
    assert parsed.items


@pytest.mark.live
def test_live_small_collection_traverses_with_cursors_until_completion() -> None:
    responses, request_error, logs = _traverse_running_api(
        "Dua-Lipa",
        max_requests=5,
    )
    assert request_error is None, [repr(request_error), logs]
    if not responses or any(response.status_code != 200 for response in responses):
        print(logs)
    assert responses and all(response.status_code == 200 for response in responses), [
        *[response.text for response in responses],
        logs,
    ]

    parsed = [SamplesResponse.model_validate(response.json()) for response in responses]
    assert len(parsed) >= 3, [result.model_dump(mode="json") for result in parsed]
    assert parsed[0].pagination.next_cursor is not None
    assert any(result.pagination.next_cursor is not None for result in parsed[1:-1])
    assert parsed[-1].pagination.next_cursor is None
    assert all(result.items for result in parsed)
    assert logs.count("visible unattended Camoufox clearance acquisition started") == 1
    assert logs.count("Samples page cache hit") == 1
    assert logs.count("browserless Samples fetch started") == len(responses) - 1

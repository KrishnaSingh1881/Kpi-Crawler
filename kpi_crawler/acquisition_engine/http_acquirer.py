"""Single-attempt HTTP acquisition: one URL, one try, fully classified result.

Retry/backoff/concurrency orchestration lives in `engine.py`; this module
only knows how to make one HTTP request and turn whatever happened into a
contract `AcquisitionState` plus enough detail for the evidence ledger.
"""

from dataclasses import dataclass
import hashlib
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .. import __version__
from ..errors import OperationalLimitError
from .contract import AcquisitionState, RedirectHop
from .proxy import ProxyConfig

USER_AGENT = f"kpi-crawler-acquisition-engine/{__version__}"

_RATE_LIMIT_STATUSES = {429}
_ACCESS_DENIED_STATUSES = {401, 403}
_TIMEOUT_STATUSES = {408}


@dataclass(frozen=True)
class HttpAttemptResult:
    success: bool
    content: bytes | None
    content_type: str | None
    http_status: int | None
    final_url: str | None
    redirect_chain: tuple[RedirectHop, ...]
    latency_ms: float
    checksum: str | None
    state: AcquisitionState
    failure_classification: str | None
    error_message: str | None


class _RecordingRedirectHandler(HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.hops: list[RedirectHop] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - stdlib override
        self.hops.append(RedirectHop(url=newurl, status=code))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_bounded(response, max_bytes: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise OperationalLimitError(f"declared content length {declared} exceeds {max_bytes} bytes")
        except ValueError:
            pass
    content = response.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise OperationalLimitError(f"content exceeds {max_bytes}-byte limit")
    return content


def _content_type(content: bytes, header_content_type: str | None) -> str:
    if header_content_type and header_content_type != "application/octet-stream":
        return header_content_type
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"


def attempt_http(
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
    proxy: ProxyConfig | None = None,
) -> HttpAttemptResult:
    redirect_handler = _RecordingRedirectHandler()
    handlers = [redirect_handler]
    if proxy is not None:
        handlers.append(ProxyHandler({"http": proxy.url, "https": proxy.url}))
    opener = build_opener(*handlers)

    started = time.monotonic()
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with opener.open(request, timeout=timeout_seconds) as response:
            content = _read_bounded(response, max_bytes)
            latency_ms = (time.monotonic() - started) * 1000
            return HttpAttemptResult(
                success=True,
                content=content,
                content_type=_content_type(content, response.headers.get_content_type()),
                http_status=response.status,
                final_url=response.geturl(),
                redirect_chain=tuple(redirect_handler.hops),
                latency_ms=latency_ms,
                checksum=hashlib.sha256(content).hexdigest(),
                state=AcquisitionState.SUCCESS,
                failure_classification=None,
                error_message=None,
            )
    except HTTPError as exc:
        latency_ms = (time.monotonic() - started) * 1000
        code = exc.code
        exc.close()
        if code in _RATE_LIMIT_STATUSES:
            state, classification = AcquisitionState.RATE_LIMITED, "http_429"
        elif code in _ACCESS_DENIED_STATUSES:
            state, classification = AcquisitionState.ACCESS_DENIED, f"http_{code}"
        elif code in _TIMEOUT_STATUSES:
            state, classification = AcquisitionState.TIMEOUT, f"http_{code}"
        else:
            state, classification = AcquisitionState.NETWORK_ERROR, f"http_{code}"
        return HttpAttemptResult(
            success=False,
            content=None,
            content_type=None,
            http_status=code,
            final_url=None,
            redirect_chain=tuple(redirect_handler.hops),
            latency_ms=latency_ms,
            checksum=None,
            state=state,
            failure_classification=classification,
            error_message=str(exc),
        )
    except OperationalLimitError as exc:
        latency_ms = (time.monotonic() - started) * 1000
        return HttpAttemptResult(
            success=False,
            content=None,
            content_type=None,
            http_status=None,
            final_url=None,
            redirect_chain=tuple(redirect_handler.hops),
            latency_ms=latency_ms,
            checksum=None,
            state=AcquisitionState.BLOCKED,
            failure_classification="artifact_too_large",
            error_message=str(exc),
        )
    except (socket.timeout, TimeoutError) as exc:
        latency_ms = (time.monotonic() - started) * 1000
        return HttpAttemptResult(
            success=False,
            content=None,
            content_type=None,
            http_status=None,
            final_url=None,
            redirect_chain=tuple(redirect_handler.hops),
            latency_ms=latency_ms,
            checksum=None,
            state=AcquisitionState.TIMEOUT,
            failure_classification="connect_timeout",
            error_message=str(exc),
        )
    except URLError as exc:
        latency_ms = (time.monotonic() - started) * 1000
        return HttpAttemptResult(
            success=False,
            content=None,
            content_type=None,
            http_status=None,
            final_url=None,
            redirect_chain=tuple(redirect_handler.hops),
            latency_ms=latency_ms,
            checksum=None,
            state=AcquisitionState.NETWORK_ERROR,
            failure_classification="connection_failed",
            error_message=str(exc),
        )

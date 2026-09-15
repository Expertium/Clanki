# Copyright: Ankitects Pty Ltd and contributors
# Copyright 2016-2021 Alex Yatskov (AnkiConnect)
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The HTTP side of Clanki's built-in AnkiConnect (spec
ankiconnect.http-protocol).

The request parsing, the CORS check, the 403 answer and the response bytes
follow the AnkiConnect add-on's web.py (GPLv3 or later) exactly, since
existing clients depend on them. What differs is where the work happens:
the add-on polled its sockets from a Qt timer on the main thread; here a
listener thread accepts connections and each connection gets a thread of
its own that reads the request, calls the handler and writes the reply, so
the socket I/O never runs on the main thread.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

# how long a connection may stay silent while its request is read, as in
# the add-on (sock.settimeout(5.0))
CLIENT_TIMEOUT_SECS = 5.0
# how often a failed listen is tried again
RETRY_SECS = 5.0
# connections served at once; more wait in the listen backlog
MAX_CLIENTS = 32
LISTEN_BACKLOG = 16
RECV_SIZE = 65536

request_schema = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "minLength": 1},
        "version": {"type": "integer"},
        "params": {"type": "object"},
    },
    "required": ["action"],
}


@dataclass(frozen=True)
class WebRequest:
    method: bytes | None
    headers: dict[bytes, bytes | None]
    body: bytes


def parse_request(data: bytes) -> tuple[WebRequest | None, int]:
    """A complete request at the start of `data` and its length, or
    (None, 0) while more bytes are needed (web.py WebClient.parseRequest)."""

    parts = data.split(b"\r\n\r\n", 1)
    if len(parts) == 1:
        return None, 0

    lines = parts[0].split(b"\r\n")
    method = None
    if len(lines) > 0:
        request_line_parts = lines[0].split(b" ")
        method = request_line_parts[0].upper() if len(request_line_parts) > 0 else None

    headers: dict[bytes, bytes | None] = {}
    for line in lines[1:]:
        pair = line.split(b": ")
        headers[pair[0].lower()] = pair[1] if len(pair) > 1 else None

    header_length = len(parts[0]) + 4
    body_length = int(headers.get(b"content-length") or 0)
    total_length = header_length + body_length
    if total_length > len(data):
        return None, 0

    return WebRequest(method, headers, data[header_length:total_length]), total_length


def allow_origin(
    headers: dict[bytes, bytes | None],
    cors_origins: Sequence[str],
    extra_origin: str | None = None,
) -> tuple[bool, str]:
    """Whether the request's origin may use AnkiConnect, and the value of
    the Access-Control-Allow-Origin header (web.py WebServer.allowOrigin).
    A request without an Origin header (not a web page) is always allowed.
    `extra_origin` is the deprecated single `webCorsOrigin`."""

    origin_list = list(cors_origins)
    if extra_origin:
        origin_list.append(extra_origin)

    allowed = False
    cors_origin = "http://localhost"
    if "*" in origin_list:
        cors_origin = "*"
        allowed = True
    elif b"origin" in headers:
        origin = (headers[b"origin"] or b"").decode()
        if origin in origin_list:
            cors_origin = origin
            allowed = True
        elif "http://localhost" in origin_list and (
            origin in ("http://127.0.0.1", "https://127.0.0.1")
            # the add-on tests this prefix twice, so https://127.0.0.1:port
            # is not admitted; kept as it is
            or origin.startswith("http://127.0.0.1:")
            or origin.startswith("chrome-extension://")
            or origin.startswith("moz-extension://")
            or origin.startswith("safari-web-extension://")
        ):
            cors_origin = origin
            allowed = True
    else:
        allowed = True
    return allowed, cors_origin


def build_headers(cors_origin: str, body: bytes) -> list[tuple[str, str | None]]:
    return [
        ("HTTP/1.1 200 OK", None),
        ("Content-Type", "application/json"),
        ("Access-Control-Allow-Origin", cors_origin),
        ("Access-Control-Allow-Headers", "*"),
        ("Content-Length", str(len(body))),
    ]


def build_response(headers: Sequence[tuple[str, str | None]], body: bytes) -> bytes:
    out = bytearray()
    for key, value in headers:
        if value is None:
            out += f"{key}\r\n".encode()
        else:
            out += f"{key}: {value}\r\n".encode()
    out += b"\r\n"
    out += body
    return bytes(out)


def format_success_reply(api_version: int, result: Any) -> Any:
    if api_version <= 4:
        return result
    return {"result": result, "error": None}


def format_exception_reply(_api_version: int, exception: BaseException) -> Any:
    return {"result": None, "error": str(exception)}


@dataclass(frozen=True)
class WebSettings:
    """What the HTTP side needs to know; a new object replaces the old one
    when the settings change."""

    bind_address: str
    bind_port: int
    cors_origins: tuple[str, ...]
    # the deprecated single origin (the add-on's `webCorsOrigin`, also read
    # from the ANKICONNECT_CORS_ORIGIN environment variable)
    extra_origin: str | None
    api_version: int = 6


def respond(
    request: WebRequest,
    settings: WebSettings,
    handler: Callable[[dict[str, Any]], Any],
) -> bytes:
    """The response bytes for one request (web.py WebServer.handlerWrapper).
    `handler` gets the decoded JSON request and returns the reply object."""

    import jsonschema

    allowed, cors_origin = allow_origin(
        request.headers, settings.cors_origins, settings.extra_origin
    )

    if request.method == b"OPTIONS":
        body = b""
        headers = build_headers(cors_origin, body)
        if request.headers.get(b"access-control-request-private-network") == b"true":
            # a public origin in the list must not fail the browser's
            # private-network check
            headers.append(("Access-Control-Allow-Private-Network", "true"))
        return build_response(headers, body)

    try:
        params = json.loads(request.body.decode("utf-8"))
        jsonschema.validate(params, request_schema)
    except (ValueError, jsonschema.ValidationError) as e:
        if allowed:
            if len(request.body) == 0:
                body = json.dumps(
                    {"apiVersion": f"AnkiConnect v.{settings.api_version}"}
                ).encode("utf-8")
            else:
                reply = format_exception_reply(settings.api_version, e)
                body = json.dumps(reply).encode("utf-8")
            return build_response(build_headers(cors_origin, body), body)
        params = {}  # the 403 below

    if allowed or params.get("action", "") == "requestPermission":
        if params.get("action", "") == "requestPermission":
            params["params"] = params.get("params", {})
            params["params"]["allowed"] = allowed
            origin = request.headers.get(b"origin")
            params["params"]["origin"] = origin.decode() if origin else ""
            if not allowed:
                cors_origin = params["params"]["origin"]
        reply = handler(params)
        try:
            body = json.dumps(reply).encode("utf-8")
        except (TypeError, ValueError) as e:
            # a result JSON cannot carry: an error reply instead of no reply
            body = json.dumps(format_exception_reply(settings.api_version, e)).encode(
                "utf-8"
            )
        headers = build_headers(cors_origin, body)
    else:
        headers = [
            ("HTTP/1.1 403 Forbidden", None),
            ("Access-Control-Allow-Origin", cors_origin),
            ("Access-Control-Allow-Headers", "*"),
        ]
        body = b""
    return build_response(headers, body)


class ServerState(Enum):
    OFF = "off"
    STARTING = "starting"
    LISTENING = "listening"
    PORT_IN_USE = "port_in_use"
    ERROR = "error"


@dataclass(frozen=True)
class ServerStatus:
    state: ServerState
    address: str = ""
    port: int = 0
    error: str = ""


def _address_family(address: str) -> socket.AddressFamily:
    return socket.AF_INET6 if ":" in address else socket.AF_INET


def _is_address_in_use(error: OSError) -> bool:
    import errno

    return error.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)) or (
        getattr(error, "winerror", None) in (10048, 10013)
    )


class WebServer:
    """Listens on one address and port on a thread of its own and answers
    each connection on another thread. `handler` gets the decoded JSON of a
    request and returns the reply object (it runs on the connection thread).

    A listen that fails (the port in use, the address missing) is tried
    again every RETRY_SECS until it succeeds or the server stops;
    `on_status` is told each change of state."""

    def __init__(
        self,
        settings: WebSettings,
        handler: Callable[[dict[str, Any]], Any],
        on_status: Callable[[ServerStatus], None] | None = None,
    ) -> None:
        self._settings = settings
        self._handler = handler
        self._on_status = on_status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._clients = threading.BoundedSemaphore(MAX_CLIENTS)
        self.status = ServerStatus(ServerState.OFF)

    @property
    def settings(self) -> WebSettings:
        return self._settings

    def update_settings(self, settings: WebSettings) -> None:
        """New CORS origins take effect with the next request; a new
        address or port needs a restart (the caller's job)."""
        self._settings = settings

    @property
    def port(self) -> int:
        """The port listened on (useful with port 0), 0 when not listening."""
        sock = self._sock
        if sock is None:
            return 0
        try:
            return int(sock.getsockname()[1])
        except OSError:
            return 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        if self._sock is None:
            self._set_status(
                ServerStatus(
                    ServerState.STARTING,
                    self._settings.bind_address,
                    self._settings.bind_port,
                )
            )
        self._thread = threading.Thread(
            target=self._run, name="AnkiConnect listener", daemon=True
        )
        self._thread.start()

    def listen_now(self) -> None:
        """Bind on the calling thread, raising on failure (tests), then serve."""
        self._sock = self._bind()
        self._set_status(
            ServerStatus(ServerState.LISTENING, self._settings.bind_address, self.port)
        )
        self.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Stop listening. Connections already being answered finish on
        their own threads; this does not wait for them."""
        self._stop.set()
        self._close_socket()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        # a socket the listener bound while stopping
        self._close_socket()
        self._set_status(ServerStatus(ServerState.OFF))

    def _close_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # listener thread

    def _bind(self) -> socket.socket:
        address, port = self._settings.bind_address, self._settings.bind_port
        sock = socket.socket(_address_family(address), socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                # SO_REUSEADDR on Windows would let two programs share the
                # port; ask for it alone instead
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    getattr(socket, "SO_EXCLUSIVEADDRUSE", -5),
                    1,
                )
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
            sock.listen(LISTEN_BACKLOG)
            sock.settimeout(0.5)
        except BaseException:
            sock.close()
            raise
        return sock

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._sock is None:
                try:
                    new_sock = self._bind()
                except OSError as error:
                    state = (
                        ServerState.PORT_IN_USE
                        if _is_address_in_use(error)
                        else ServerState.ERROR
                    )
                    self._set_status(
                        ServerStatus(
                            state,
                            self._settings.bind_address,
                            self._settings.bind_port,
                            error.strerror or str(error),
                        )
                    )
                    self._stop.wait(RETRY_SECS)
                    continue
                if self._stop.is_set():
                    # stopped while binding: the port must not stay taken
                    new_sock.close()
                    break
                self._sock = new_sock
                self._set_status(
                    ServerStatus(
                        ServerState.LISTENING,
                        self._settings.bind_address,
                        self.port,
                    )
                )
            sock = self._sock
            if sock is None:
                continue
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            self._clients.acquire()
            threading.Thread(
                target=self._serve_client,
                args=(conn,),
                name="AnkiConnect client",
                daemon=True,
            ).start()

    # connection threads

    def _serve_client(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(CLIENT_TIMEOUT_SECS)
            request = self._read_request(conn)
            if request is None:
                return
            conn.sendall(respond(request, self._settings, self._handler))
        except (OSError, ValueError):
            # a dropped or silent connection, or a malformed content-length
            pass
        except Exception:
            import traceback

            traceback.print_exc()
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._clients.release()

    @staticmethod
    def _read_request(conn: socket.socket) -> WebRequest | None:
        data = bytearray()
        needed: int | None = None
        while True:
            chunk = conn.recv(RECV_SIZE)
            if not chunk:
                return None
            data += chunk
            if needed is None:
                end = data.find(b"\r\n\r\n")
                if end < 0:
                    continue
                # the header block is complete: its content-length tells how
                # much more to read, so the request is parsed once, at the end
                # (the add-on re-parsed the whole buffer after every 1 KB)
                needed = end + 4 + _content_length(bytes(data[:end]))
            if len(data) >= needed:
                return parse_request(bytes(data))[0]

    def _set_status(self, status: ServerStatus) -> None:
        self.status = status
        if self._on_status is not None:
            try:
                self._on_status(status)
            except Exception:
                pass


def _content_length(header_block: bytes) -> int:
    """The content-length of a header block, read as parse_request reads it
    (the last such header wins)."""
    length = b""
    for line in header_block.split(b"\r\n")[1:]:
        pair = line.split(b": ")
        if pair[0].lower() == b"content-length":
            length = pair[1] if len(pair) > 1 else b""
    return int(length or 0)

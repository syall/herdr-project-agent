"""The one network path of pa-costs (t-032 J2): the bounded download of a declared public price list for
`pa-costs prices fetch`, the person's command. Kept out of pa_costs.py so the collector, the summary and the status
line stay free of any network module (t-009); pa_costs.py loads this file only for a fetch. The standard library's
urllib, http.client, socket and ssl are imported inside the functions, never at module load.

What it does and does not do (spec R-S1 to R-S3): one HTTPS GET per declared URL through an opener built by hand
(https with certificate checks, no redirect, no plain http, file, ftp or data, no cookie, no authentication); only
`User-Agent: herdr-pa-costs` and `Accept-Encoding: identity` are sent; the body is streamed into a new file and refused
past its maxBytes, past the whole fetch's deadline, or short of its Content-Length (t-070 J2 R11). The proxy is
HTTPS_PROXY alone, `http://host:port` with no user name or password; no other proxy variable and no system proxy setting
is read (t-070 J2 R9). With the fetch's deadline every blocking step waits at most the budget left: each address's TCP
connect, the proxy's CONNECT request and reply, the TLS handshake and every TLS read and write (t-070 J2 R8; J2 round 2,
REVIEW M3); the body is read one socket read at a time with the clock checked before each. Not bounded by the budget:
the name lookup (the system resolver's own timeouts). Nothing here reads a credential, a credential file or a cookie
store."""

CONNECT_TIMEOUT = 20  # seconds one blocking network operation may take
CHUNK = 65536
PROXY_RULE = "HTTPS_PROXY must be http://host:port or host:port"
HOST_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789.-_")


def read_timeout(deadline, clock, fail, budget):
    """The seconds one blocking read may wait: CONNECT_TIMEOUT or the budget left, the smaller; raises fail(3) once the
    clock has passed the deadline."""
    left = deadline - clock()
    if left <= 0:
        raise fail(3, "the downloads took longer than {} s: stopped".format(budget))
    return min(CONNECT_TIMEOUT, left)


def bounded_socket(base, deadline, clock, fail, budget):
    """A subclass of the socket class base whose every blocking read, write and (TLS) handshake first sets the socket's
    timeout to read_timeout: a peer sending or taking a byte at a time cannot hold the fetch past its deadline. The
    parent's method is looked up at each call."""
    def bounded(name):
        def method(self, *args, **kwargs):
            self.settimeout(read_timeout(deadline, clock, fail, budget))
            return getattr(base, name)(self, *args, **kwargs)
        method.__name__ = name
        return method
    names = [n for n in ("recv_into", "recv", "read", "send", "sendall", "do_handshake") if hasattr(base, n)]
    return type("Bounded" + base.__name__[:1].upper() + base.__name__[1:], (base,), {n: bounded(n) for n in names})


def connector(deadline, clock, fail, budget, resolve=None, make=None):
    """socket.create_connection's loop, bounded: each address of the name is tried with a timeout of read_timeout (and
    the connection's own, the smaller), on a bounded plain socket (so a proxy's CONNECT reply is bounded too); past the
    deadline no lookup and no further address. The last error is raised when every address fails."""
    def bounded_connect(address, timeout=None, source_address=None):
        import socket
        read_timeout(deadline, clock, fail, budget)
        sock_class = make or bounded_socket(socket.socket, deadline, clock, fail, budget)
        error = None
        for family, kind, proto, _name, where in (resolve or socket.getaddrinfo)(address[0], address[1], 0, socket.SOCK_STREAM):
            wait = read_timeout(deadline, clock, fail, budget)
            if isinstance(timeout, (int, float)):
                wait = min(wait, timeout)
            sock = None
            try:
                sock = sock_class(family, kind, proto)
                sock.settimeout(wait)
                if source_address:
                    sock.bind(source_address)
                sock.connect(where)
                return sock
            except OSError as e:
                error = e
                if sock is not None:
                    sock.close()
        raise error if error is not None else OSError("no address for {}".format(address[0]))
    return bounded_connect


def https_proxy(environ):
    """(host:port, None) from HTTPS_PROXY, (None, None) when it is not set, (None, why) when it is refused: another
    scheme, a user name or password, a path or query, no port. The value itself is never quoted back (R9)."""
    value = (environ.get("HTTPS_PROXY") or "").strip()
    if not value:
        return None, None
    rest = value
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
        if scheme.lower() != "http":
            return None, PROXY_RULE + " (a plain http proxy; the fetch's TLS runs through its tunnel)"
    if "@" in rest:
        return None, "HTTPS_PROXY holds a user name or password: no proxy credential is sent. " + PROXY_RULE
    host, sep, port = (rest[:-1] if rest.endswith("/") else rest).rpartition(":")
    ipv6 = host.startswith("[") and host.endswith("]") and set(host[1:-1].lower()) <= set("0123456789abcdef:")
    if not sep or not port or not set(port) <= set("0123456789") or not 0 < int(port) < 65536 or not host \
            or not (ipv6 or set(host.lower()) <= HOST_CHARS):
        return None, PROXY_RULE
    return "{}:{}".format(host, int(port)), None


def http_opener(no_redirect, deadline=None, clock=None, fail=None, budget=None, environ=None):
    """The only opener the fetch uses: HTTPS_PROXY alone (R9), https with the default certificate checks, the given
    handler refusing every redirect, the default error handling. Nothing else. With a deadline, the connection's TCP
    connect and the socket before TLS (connector), the TLS handshake and every TLS read and write (the context's socket
    class) wait at most the budget left (R8, R-S3). A refused HTTPS_PROXY is fail(1) before anything is opened."""
    import os
    import ssl
    import urllib.request
    hostport, why = https_proxy(os.environ if environ is None else environ)
    if why:
        raise fail(1, why) if fail else ValueError(why)
    context = ssl.create_default_context()
    if deadline is None:
        https = urllib.request.HTTPSHandler(context=context)
    else:
        context.sslsocket_class = bounded_socket(ssl.SSLSocket, deadline, clock, fail, budget)
        https = bounded_https(context, connector(deadline, clock, fail, budget))
    handlers = [https, no_redirect, urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()]
    if hostport:
        handlers.insert(0, proxy_only_https(hostport))
    opener = urllib.request.OpenerDirector()
    for h in handlers:
        opener.add_handler(h)
    return opener


def proxy_only_https(hostport):
    """The proxy handler: every https request tunnels through hostport (CONNECT); no header is added (R9)."""
    import urllib.request

    class ProxyOnlyHttps(urllib.request.BaseHandler):
        handler_order = 100  # before the https handler, as urllib's own proxy handler

        def https_open(self, req):
            if not req.has_proxy():
                req.set_proxy(hostport, "https")
            return None
    return ProxyOnlyHttps()


def bounded_https(context, connect):
    """An https handler whose connections are made by connect (connector's bounded_connect) instead of
    socket.create_connection: http.client's HTTPConnection calls self._create_connection for the TCP connection."""
    import http.client
    import urllib.request

    class BoundedConnection(http.client.HTTPSConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = connect

    class BoundedHTTPSHandler(urllib.request.HTTPSHandler):
        connection_class = BoundedConnection

        def do_open(self, http_class, req, **kwargs):
            return super().do_open(self.connection_class, req, **kwargs)
    return BoundedHTTPSHandler(context=context)


def described(e):
    """An error's text for a message: http.client's own errors (no OSError) by their class name too (R10)."""
    import http.client
    if isinstance(e, http.client.HTTPException):
        return "{}: {}".format(type(e).__name__, e)
    return str(getattr(e, "reason", None) or e)


def download(url, max_bytes, dest, deadline, opener, clock, fail, scrub, budget):
    """GET url into dest (a new file, 0600) through opener(request, timeout): at most max_bytes, before clock() passes
    deadline, and all of its Content-Length. Returns {"bytes", "sha256"}; raises fail(code, why): 3 past the deadline, 1
    any other failure (http.client's errors too: no traceback, R10)."""
    import hashlib
    import http.client
    import os
    import urllib.error
    import urllib.request
    left = deadline - clock()
    if left <= 0:
        raise fail(3, "the downloads took longer than {} s: stopped".format(budget))
    req = urllib.request.Request(url, headers={"User-Agent": "herdr-pa-costs", "Accept-Encoding": "identity"})
    try:
        resp = opener(req, min(CONNECT_TIMEOUT, left))
    except fail:
        raise
    except urllib.error.HTTPError as e:
        raise fail(1, "{} answered HTTP {}".format(url, e.code))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        if clock() > deadline:  # a connect or header read that timed out at the budget's end (t-032 REVIEW N4)
            raise fail(3, "the downloads took longer than {} s: stopped".format(budget))
        text = scrub(described(e))[:200]
        hint = " (this Python has no certificate store: install its certificates; checks are never turned off)" \
            if "CERTIFICATE_VERIFY_FAILED" in text else ""
        raise fail(1, "download of {} failed: {}{}".format(url, text, hint))
    digest, total = hashlib.sha256(), 0
    try:
        status = int(getattr(resp, "status", None) or resp.getcode())
        headers = resp.headers
        if 300 <= status < 400:
            raise fail(1, "redirect (HTTP {}) to {} refused: only the declared URL is fetched".format(
                status, scrub(str(headers.get("Location") or "?"))[:200]))
        if status != 200:
            raise fail(1, "{} answered HTTP {}".format(url, status))
        encoding = (headers.get("Content-Encoding") or "identity").strip().lower()
        if encoding != "identity":
            raise fail(1, "{} came compressed ({}): refused".format(url, encoding[:20]))
        length = headers.get("Content-Length")
        try:
            length = int(length) if length is not None else None
        except ValueError:
            length = None
        if length is not None and length > max_bytes:
            raise fail(1, "{} is {} bytes, more than its maxBytes {}".format(url, length, max_bytes))
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # read1: one socket read per call (read would loop inside a BufferedReader until CHUNK bytes), so the clock is
        # checked before every socket read, not only between chunks.
        read = getattr(resp, "read1", None) or resp.read
        with os.fdopen(fd, "wb") as out:
            while True:
                read_timeout(deadline, clock, fail, budget)
                chunk = read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise fail(1, "{} sent more than its maxBytes {}".format(url, max_bytes))
                digest.update(chunk)
                out.write(chunk)
        # R11: a body cut short is refused (with Transfer-Encoding the length is not the body's, RFC 9112 6.3)
        if length is not None and not headers.get("Transfer-Encoding") and total != length:
            raise fail(1, "{} ended after {} of its {} bytes (Content-Length): refused".format(url, total, length))
    except (OSError, ValueError, http.client.HTTPException) as e:
        if clock() > deadline:  # a read that timed out at the budget's end
            raise fail(3, "the downloads took longer than {} s: stopped".format(budget))
        raise fail(1, "download of {} failed: {}".format(url, scrub(described(e))[:200]))
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
    return {"bytes": total, "sha256": digest.hexdigest()}

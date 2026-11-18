"""The one network path of pa-costs (t-032 J2): the bounded download of a declared public price list for
`pa-costs prices fetch`, the person's command. Kept out of pa_costs.py so the collector, the summary and the status
line stay free of any network module (t-009); pa_costs.py loads this file only for a fetch. The standard library's
urllib and ssl are imported inside the functions, never at module load.

What it does and does not do (spec R-S1 to R-S3): one HTTPS GET per declared URL through an opener built by hand
(environment proxies, https with certificate checks, no redirect, no plain http, file, ftp or data, no cookie, no
authentication); only `User-Agent: herdr-pa-costs` and `Accept-Encoding: identity` are sent; the body is streamed into
a new file and refused past its maxBytes or past the whole fetch's deadline. Nothing here reads a credential, a
credential file or a cookie store."""

CONNECT_TIMEOUT = 20  # seconds one blocking network operation may take
CHUNK = 65536


def http_opener(no_redirect):
    """The only opener the fetch uses: environment proxies (Q5), https with the default certificate checks, the given
    handler refusing every redirect, the default error handling. Nothing else."""
    import ssl
    import urllib.request
    opener = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler(), urllib.request.HTTPSHandler(context=ssl.create_default_context()), no_redirect,
              urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        opener.add_handler(h)
    return opener


def download(url, max_bytes, dest, deadline, opener, clock, fail, scrub, budget):
    """GET url into dest (a new file, 0600) through opener(request, timeout): at most max_bytes, before clock() passes
    deadline. Returns {"bytes", "sha256"}; raises fail(code, why): 3 past the deadline, 1 any other failure."""
    import hashlib
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
    except (urllib.error.URLError, OSError, ValueError) as e:
        text = scrub(str(getattr(e, "reason", None) or e))[:200]
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
        with os.fdopen(fd, "wb") as out:
            while True:
                if clock() > deadline:
                    raise fail(3, "the downloads took longer than {} s: stopped".format(budget))
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise fail(1, "{} sent more than its maxBytes {}".format(url, max_bytes))
                digest.update(chunk)
                out.write(chunk)
    except (OSError, ValueError) as e:
        raise fail(1, "download of {} failed: {}".format(url, scrub(str(e))[:200]))
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
    return {"bytes": total, "sha256": digest.hexdigest()}

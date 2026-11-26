from urllib.parse import unquote_plus, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_WARFRAME_HOST = "www.warframe.com"


def _is_tracking(segment: str) -> bool:
    key = unquote_plus(segment.split("=", 1)[0]).lower()
    return key.startswith("utm_") or key in _TRACKING_KEYS


def canonicalize_url(url: str) -> str:
    stripped = url.strip()
    try:
        parts = urlsplit(stripped)
        port = parts.port
    except ValueError:
        return stripped
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    userinfo, at, _ = parts.netloc.rpartition("@")
    authority = f"{userinfo}{at}{host}"
    netloc = (
        authority if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{authority}:{port}"
    )
    path = parts.path
    if host == _WARFRAME_HOST and path.startswith("/en/news/"):
        path = path[len("/en") :]
    kept = [s for s in parts.query.split("&") if s and not _is_tracking(s)]
    return urlunsplit((scheme, netloc, path, "&".join(kept), ""))

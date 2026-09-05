#!/usr/bin/env python3
"""GitHub Actions entrypoint for the harvester.

André Louis' archive currently serves a TLS certificate whose hostname does not
match onj3.andrelouis.com.  We keep normal TLS verification everywhere else and
only relax hostname/certificate verification for that one archive host.
"""
from __future__ import annotations

import ssl
import urllib.parse
import urllib.request

ARCHIVE_HOST = "onj3.andrelouis.com"
_ORIGINAL_URLOPEN = urllib.request.urlopen
_ARCHIVE_SSL_CONTEXT = ssl._create_unverified_context()


def _archive_aware_urlopen(url, *args, **kwargs):
    target = url.full_url if isinstance(url, urllib.request.Request) else str(url)
    host = urllib.parse.urlparse(target).hostname
    if host == ARCHIVE_HOST and "context" not in kwargs:
        kwargs["context"] = _ARCHIVE_SSL_CONTEXT
    return _ORIGINAL_URLOPEN(url, *args, **kwargs)


urllib.request.urlopen = _archive_aware_urlopen

import phone_defaults  # noqa: E402  (patch urllib before loading the harvester)


if __name__ == "__main__":
    raise SystemExit(phone_defaults.main())

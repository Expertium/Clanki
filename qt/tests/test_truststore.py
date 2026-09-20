# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""aqt's truststore injection leaves the same end state as
truststore.inject_into_ssl() without importing requests at start-up."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _run(code: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_every_tls_context_uses_the_system_store_without_importing_requests() -> None:
    out = _run(
        """
        import sys
        import aqt
        import ssl, truststore
        print("requests" in sys.modules, "urllib3" in sys.modules)
        print(ssl.SSLContext is truststore.SSLContext)
        print(isinstance(ssl.create_default_context(), truststore.SSLContext))
        import urllib3.util.ssl_ as u, requests.adapters as ra
        print(u.SSLContext is truststore.SSLContext)
        print(isinstance(u.create_urllib3_context(), truststore.SSLContext))
        pre = getattr(ra, "_preloaded_ssl_context", None)
        print(pre is None or isinstance(pre, truststore.SSLContext))
        """
    )
    assert out.split() == ["False", "False"] + ["True"] * 5


def test_a_requests_imported_before_aqt_is_patched_too() -> None:
    out = _run(
        """
        import urllib3.util.ssl_ as u, requests
        import aqt
        import truststore
        print(u.SSLContext is truststore.SSLContext)
        """
    )
    assert out == "True"

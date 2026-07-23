"""Authentication for the bus's write surface (ARCHITECTURE.md §11, §14 #23).

Pure functions only — a header string and a secret in, a verdict out — so the
whole auth decision sits in the mutation gate with no sockets and no framework.
`server.py` supplies the real request headers.

**Why the cost page needed none of this and the policy page does.** Reading the
ledger is harmless enough that the tailnet boundary carries it. Changing which
model a project runs on has money behind it: by milamber's own table `gpt-5.5-pro`
is 30/180 per Mtok against `gpt-5-nano`'s 0.05/0.40, roughly 600x on input. One
wrong dropdown makes every project expensive silently, and the tailnet is not a
single-person network.

**HTTP Basic, over plain HTTP, deliberately.** Basic sends base64 — encoding, not
encryption — so on its own it would be indefensible. It is acceptable here for
exactly one reason: the transport is Tailscale (WireGuard-encrypted) and the
listener never binds a public interface (`config.validate_costs_hosts` refuses the
wildcard). If either of those ever changes, this becomes a plaintext credential on
the wire. It is chosen over a login form + session cookie because the browser
prompts natively and there is no session state to get wrong.

**Basic auth is why CSRF matters here.** Browsers cache Basic credentials and
re-send them automatically, so a page on another origin could POST to this one and
the browser would attach the credentials. `origin_allowed` closes that: a
state-changing request must either carry no `Origin` (a plain form post from the
page itself, or curl) or carry one matching the host it is talking to.
"""

from __future__ import annotations

import base64
import binascii
import hmac

# The realm the browser shows in its credential prompt.
BASIC_REALM = "llmbus policy"


def basic_auth_ok(header: str | None, secret: str | None) -> bool:
    """True when `header` carries the shared secret as HTTP Basic credentials.

    `secret` of `None` is always False — an unconfigured page cannot be entered,
    which is what makes the missing-secret case fail closed rather than open.

    Only the password half is checked; any username is accepted. The secret is a
    shared password, not an account, and pretending otherwise would invite someone
    to think usernames mean something here.

    Comparison is `hmac.compare_digest`, not `==`: a byte-by-byte comparison
    leaks the length of the matching prefix through timing. Same reasoning as the
    callback signature check (§14 #19).
    """
    if secret is None or header is None:
        return False
    scheme, separator, remainder = header.partition(" ")
    if not separator or scheme.lower() != "basic":
        return False
    # RFC 7235 allows one or more spaces between the scheme and the credentials,
    # so the remainder is stripped rather than assumed to start immediately.
    encoded = remainder.strip()
    if not encoded:
        return False
    try:
        # validate=True is load-bearing, not decoration. Without it b64decode
        # silently DISCARDS characters outside the alphabet, so "eDpz!M2NyZXQ="
        # would decode to the same bytes as "eDpzM2NyZXQ=" and authenticate.
        # Credentials must be well-formed, not merely recoverable.
        # `.decode()` defaults to utf-8/strict; spelling it out would only add a
        # mutant no test could kill, since codec names are normalised.
        decoded = base64.b64decode(encoded, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return False
    _, separator, password = decoded.partition(":")
    if not separator:
        return False
    return hmac.compare_digest(password, secret)


def origin_allowed(origin: str | None, host: str | None) -> bool:
    """True when a state-changing request may proceed, given `Origin` and `Host`.

    No `Origin` is allowed: same-origin form posts from older browsers, and every
    command-line client, omit it. That is not a hole — an attacker's page cannot
    *suppress* the header a browser adds, so the absent case is not reachable from
    a cross-site form.

    Present means it must match the host being addressed, scheme and port
    included, so `http://100.124.41.86:8093` is accepted while any other origin
    is refused.
    """
    if origin is None:
        return True
    if host is None:
        return False
    return origin in (f"http://{host}", f"https://{host}")

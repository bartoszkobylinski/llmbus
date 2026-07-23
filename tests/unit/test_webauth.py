"""Unit tests for the policy page's auth (§11, §14 #23).

Pure functions, so this is all string in / verdict out. In the mutation gate, so
the assertions are exact rather than "roughly rejects bad input" — an auth check
that fails open under one mutated operator is the whole risk being guarded here.
"""

import base64

import pytest

from llmbus.webauth import BASIC_REALM, basic_auth_ok, origin_allowed


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# --- basic_auth_ok: the closed cases -----------------------------------------


def test_no_secret_configured_rejects_even_correct_looking_credentials():
    # The fail-safe that matters most: an operator who never set the secret gets a
    # page nobody can enter, NOT a page anyone can.
    assert basic_auth_ok(_basic("x", "anything"), None) is False


def test_no_secret_rejects_even_an_empty_password():
    assert basic_auth_ok(_basic("x", ""), None) is False


def test_missing_header_is_rejected():
    assert basic_auth_ok(None, "s3cret") is False


def test_empty_header_is_rejected():
    assert basic_auth_ok("", "s3cret") is False


def test_wrong_password_is_rejected():
    assert basic_auth_ok(_basic("x", "wrong"), "s3cret") is False


def test_a_password_that_is_a_prefix_of_the_secret_is_rejected():
    assert basic_auth_ok(_basic("x", "s3cre"), "s3cret") is False


def test_a_password_that_extends_the_secret_is_rejected():
    assert basic_auth_ok(_basic("x", "s3crets"), "s3cret") is False


@pytest.mark.parametrize("scheme", ["Bearer", "Digest", "Negotiate"])
def test_other_auth_schemes_are_rejected(scheme):
    encoded = base64.b64encode(b"x:s3cret").decode()
    assert basic_auth_ok(f"{scheme} {encoded}", "s3cret") is False


def test_a_scheme_with_no_credentials_is_rejected():
    assert basic_auth_ok("Basic", "s3cret") is False
    assert basic_auth_ok("Basic ", "s3cret") is False


def test_undecodable_base64_is_rejected_not_raised():
    assert basic_auth_ok("Basic !!!not-base64!!!", "s3cret") is False


def test_base64_of_non_utf8_bytes_is_rejected_not_raised():
    encoded = base64.b64encode(b"\xff\xfe\xfd").decode()
    assert basic_auth_ok(f"Basic {encoded}", "s3cret") is False


def test_credentials_without_a_colon_are_rejected():
    # "user" alone is not "user:password"; without the separator there is no
    # password to compare, and treating the whole string as one would let
    # `s3cret` (no colon) authenticate.
    encoded = base64.b64encode(b"s3cret").decode()
    assert basic_auth_ok(f"Basic {encoded}", "s3cret") is False


# --- basic_auth_ok: the open cases -------------------------------------------


def test_correct_password_is_accepted():
    assert basic_auth_ok(_basic("x", "s3cret"), "s3cret") is True


def test_the_username_is_ignored():
    # The secret is a shared password, not an account. Pretending usernames mean
    # something would invite someone to believe they are a second factor.
    assert basic_auth_ok(_basic("bartek", "s3cret"), "s3cret") is True
    assert basic_auth_ok(_basic("", "s3cret"), "s3cret") is True


def test_the_scheme_name_is_case_insensitive():
    # RFC 7235: the scheme token is case-insensitive; curl sends "Basic", some
    # clients send "basic".
    assert basic_auth_ok(_basic("x", "s3cret").replace("Basic", "basic"), "s3cret") is True


def test_a_password_containing_a_colon_survives_intact():
    # Only the FIRST colon separates user from password, so a secret may contain
    # colons — a real risk if someone generates one with `openssl rand -base64`.
    assert basic_auth_ok(_basic("x", "a:b:c"), "a:b:c") is True


def test_the_realm_is_stable():
    # The browser shows it and remembers credentials against it; changing it
    # silently re-prompts everyone.
    assert BASIC_REALM == "llmbus policy"


# --- origin_allowed ----------------------------------------------------------


def test_absent_origin_is_allowed():
    # curl and same-origin form posts omit it. An attacker's page cannot suppress
    # the header a browser adds, so the absent case is not reachable cross-site.
    assert origin_allowed(None, "100.124.41.86:8093") is True


def test_matching_http_origin_is_allowed():
    assert origin_allowed("http://100.124.41.86:8093", "100.124.41.86:8093") is True


def test_matching_https_origin_is_allowed():
    assert origin_allowed("https://100.124.41.86:8093", "100.124.41.86:8093") is True


def test_a_foreign_origin_is_refused():
    # The CSRF case: browsers re-send cached Basic credentials automatically.
    assert origin_allowed("http://evil.example", "100.124.41.86:8093") is False


def test_a_different_port_on_the_same_host_is_refused():
    assert origin_allowed("http://100.124.41.86:9999", "100.124.41.86:8093") is False


def test_an_origin_with_no_host_to_compare_against_is_refused():
    assert origin_allowed("http://100.124.41.86:8093", None) is False


def test_a_prefix_of_the_real_origin_is_refused():
    assert origin_allowed("http://100.124.41.8", "100.124.41.86:8093") is False


def test_an_origin_that_merely_contains_the_host_is_refused():
    # Guards against a substring check: evil.com/?100.124.41.86:8093 must not pass.
    assert origin_allowed("http://evil.example/100.124.41.86:8093", "100.124.41.86:8093") is False


def test_credentials_with_junk_spliced_in_are_rejected():
    # THE reason b64decode is called with validate=True. Without it, decoding
    # silently discards characters outside the base64 alphabet, so this string
    # would decode to exactly "x:s3cret" and authenticate.
    spliced = base64.b64encode(b"x:s3cret").decode().replace("z", "z!", 1)
    assert basic_auth_ok(f"Basic {spliced}", "s3cret") is False


def test_the_same_credentials_without_the_junk_do_authenticate():
    # Pins that the string above differs from a valid one ONLY by the junk, so
    # the test above is really testing validation and not a typo.
    clean = base64.b64encode(b"x:s3cret").decode()
    assert basic_auth_ok(f"Basic {clean}", "s3cret") is True


def test_several_spaces_between_scheme_and_credentials_are_accepted():
    # RFC 7235 allows 1*SP. Splitting on the LAST space instead of the first
    # would put a space inside the scheme and reject this.
    clean = base64.b64encode(b"x:s3cret").decode()
    assert basic_auth_ok(f"Basic   {clean}", "s3cret") is True


def test_surrounding_whitespace_around_the_credentials_is_tolerated():
    clean = base64.b64encode(b"x:s3cret").decode()
    assert basic_auth_ok(f"Basic  {clean}  ", "s3cret") is True


def test_a_scheme_with_only_whitespace_after_it_is_rejected():
    assert basic_auth_ok("Basic    ", "s3cret") is False

"""
DigiKey's client-credentials token expires after 599 seconds and has no refresh token, so the
API wrapper has to fetch a new one itself. These run against a fake token endpoint and a fake
API on a fake clock; nothing here talks to DigiKey.
"""

import json
import time
from typing import Any

import pytest
from requests import PreparedRequest, Response, Session
from requests.adapters import BaseAdapter

from inventree_part_import.exceptions import SupplierError
from inventree_part_import.suppliers import supplier_digikey
from inventree_part_import.suppliers.supplier_digikey import DigiKeyApi

TOKEN_LIFETIME = 599
START = 1_000_000.0


class FakeClock:
    def __init__(self):
        self.now = START

    def __call__(self) -> float:
        return self.now


class FakeDigiKey(BaseAdapter):
    """The token endpoint and the product details endpoint, keeping score of the calls."""

    def __init__(self, clock: FakeClock):
        super().__init__()
        self.clock = clock
        self.issued = 0
        #: Access token -> when DigiKey stops accepting it.
        self.valid_until: dict[str, float] = {}
        self.api_calls: list[str | None] = []
        #: Answer the next n API calls with a 401 whatever the token.
        self.reject_next = 0
        #: Answer token requests with this status and body instead of a token.
        self.token_failure: tuple[int, dict[str, Any]] | None = None

    def send(self, request: PreparedRequest, *args: Any, **kwargs: Any) -> Response:
        if request.url == DigiKeyApi.OAUTH2_TOKEN_URL:
            if self.token_failure:
                return _response(request, *self.token_failure)
            self.issued += 1
            token = f"token-{self.issued}"
            self.valid_until[token] = self.clock.now + TOKEN_LIFETIME
            return _response(
                request,
                200,
                {"access_token": token, "expires_in": TOKEN_LIFETIME, "token_type": "Bearer"},
            )

        authorization = request.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization else None
        self.api_calls.append(token)
        if self.reject_next:
            self.reject_next -= 1
            return _response(
                request, 401, {"title": "Unauthorized", "detail": "Bearer token expired"}
            )
        if token is None or self.clock.now >= self.valid_until.get(token, 0):
            return _response(
                request, 401, {"title": "Unauthorized", "detail": "Bearer token expired"}
            )
        return _response(
            request, 200, {"Product": {"ManufacturerProductNumber": "RC0402FR-0710KL"}}
        )

    def close(self):
        pass


def _response(request: PreparedRequest, status: int, body: dict[str, Any]) -> Response:
    response = Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.headers["Content-Type"] = "application/json"
    response.request = request
    response.url = request.url or ""
    return response


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    # oauthlib reads time.time() too, for the token's expires_at and its own expiry check.
    fake = FakeClock()
    monkeypatch.setattr(time, "time", fake)
    return fake


@pytest.fixture
def digikey(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    fake = FakeDigiKey(clock)
    real_setup_session = supplier_digikey.setup_session

    def setup_session(session: Session):
        session = real_setup_session(session)
        session.mount(DigiKeyApi.BASE_URL, fake)
        return session

    monkeypatch.setattr(supplier_digikey, "setup_session", setup_session)
    return fake


def make_api():
    return DigiKeyApi("client-id", "client-secret", "USD", "en", "US")


def test_a_fresh_token_is_reused(clock: FakeClock, digikey: FakeDigiKey):
    api = make_api()
    assert digikey.issued == 1

    clock.now += TOKEN_LIFETIME - DigiKeyApi.TOKEN_MARGIN_SECONDS - 1
    assert api.product_details("RC0402FR-0710KL")
    assert digikey.issued == 1
    assert digikey.api_calls == ["token-1"]


def test_a_token_about_to_expire_is_fetched_again_first(clock: FakeClock, digikey: FakeDigiKey):
    api = make_api()

    clock.now += TOKEN_LIFETIME - DigiKeyApi.TOKEN_MARGIN_SECONDS + 1
    assert api.product_details("RC0402FR-0710KL")
    assert digikey.issued == 2
    assert digikey.api_calls == ["token-2"], "the old token is never sent"


def test_a_long_lived_api_object_keeps_working(clock: FakeClock, digikey: FakeDigiKey):
    """The bug: one DigiKeyApi per server process, searched hours after it was made."""
    api = make_api()

    for _ in range(5):
        clock.now += 3600
        assert api.product_details("RC0402FR-0710KL")
        assert api.keyword_search("RC0402FR-0710KL", limit=10)

    assert digikey.issued == 6
    assert all(token == f"token-{2 + i // 2}" for i, token in enumerate(digikey.api_calls))


def test_a_locally_expired_token_is_fetched_again_and_the_call_retried(
    clock: FakeClock, digikey: FakeDigiKey, monkeypatch: pytest.MonkeyPatch
):
    """oauthlib refuses to send an expired token itself (TokenExpiredError) before any 401."""
    api = make_api()
    # With a negative margin the early check lets an expired token through to oauthlib.
    monkeypatch.setattr(api, "TOKEN_MARGIN_SECONDS", -10)

    clock.now += TOKEN_LIFETIME + 5
    assert api.product_details("RC0402FR-0710KL")
    assert digikey.issued == 2
    assert digikey.api_calls == ["token-2"]


def test_a_401_fetches_a_new_token_and_retries_once(clock: FakeClock, digikey: FakeDigiKey):
    api = make_api()
    digikey.reject_next = 1

    assert api.product_details("RC0402FR-0710KL")
    assert digikey.issued == 2
    assert digikey.api_calls == ["token-1", "token-2"]


def test_a_second_401_is_a_supplier_error(clock: FakeClock, digikey: FakeDigiKey):
    api = make_api()
    digikey.reject_next = 2

    with pytest.raises(SupplierError) as error:
        api.product_details("RC0402FR-0710KL")

    assert "Bearer token expired (HTTP 401)" in str(error.value)
    assert digikey.issued == 2, "one new token, not a loop"
    assert len(digikey.api_calls) == 2


def test_a_token_that_cannot_be_renewed_is_a_supplier_error(clock: FakeClock, digikey: FakeDigiKey):
    api = make_api()
    digikey.token_failure = (401, {"error": "invalid_client", "error_description": "bad secret"})

    clock.now += TOKEN_LIFETIME
    with pytest.raises(SupplierError) as error:
        api.keyword_search("RC0402FR-0710KL", limit=10)

    assert "failed to renew the OAuth token" in str(error.value)
    assert "invalid_client" in str(error.value)
    assert "client-secret" not in str(error.value)
    assert digikey.api_calls == [], "nothing is sent with the expired token"

    digikey.token_failure = None
    assert api.keyword_search("RC0402FR-0710KL", limit=10), "and it recovers once DigiKey does"


def test_the_token_is_fetched_once_for_concurrent_renewals(clock: FakeClock, digikey: FakeDigiKey):
    """A renewal that finds the token already replaced (by another thread) fetches nothing."""
    api = make_api()
    stale = api._access_token  # pyright: ignore[reportPrivateUsage]
    api._renew_token(unless_changed_from=stale)  # pyright: ignore[reportPrivateUsage]
    api._renew_token(unless_changed_from=stale)  # pyright: ignore[reportPrivateUsage]
    assert digikey.issued == 2

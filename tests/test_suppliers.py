from pathlib import Path
from typing import Any

import pytest

from inventree_part_import import config
from inventree_part_import.exceptions import SupplierError, SupplierLoadError
from inventree_part_import.suppliers import (
    available_supplier_ids,
    create_supplier,
    discover_supplier_classes,
)
from inventree_part_import.suppliers import supplier_digikey
from inventree_part_import.suppliers.base import ApiPart, Supplier, SupplierSupportLevel
from inventree_part_import.suppliers.supplier_digikey import DigiKey, DigiKeyApi


class FakeDigiKeyApi:
    def __init__(self, *args: Any, **kwargs: Any):
        self.args = args


@pytest.fixture
def offline_digikey(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(supplier_digikey, "DigiKeyApi", FakeDigiKeyApi)


def test_every_supplier_module_is_discovered():
    ids = available_supplier_ids()
    assert {"digikey", "lcsc", "mouser", "reichelt", "tme"} <= set(ids)
    assert all(issubclass(cls, Supplier) for cls in discover_supplier_classes().values())


def test_create_supplier_builds_from_explicit_parameters(
    config_dir: Path, no_terminal: None, offline_digikey: None
):
    supplier = create_supplier(
        "digikey",
        client_id="id",
        client_secret="secret",
        currency="USD",
        language="en",
        location="us",
        interactive_part_matches=10,
    )

    assert isinstance(supplier, DigiKey)
    assert supplier.limit == 10
    assert not (config_dir / config.SUPPLIERS_CONFIG).exists(), "nothing is written"


def test_create_supplier_falls_back_to_the_global_config(
    config_dir: Path, no_terminal: None, offline_digikey: None
):
    config.set_config({"currency": "USD", "language": "en", "location": "US"})

    supplier = create_supplier("digikey", client_id="id", client_secret="secret")

    assert isinstance(supplier, DigiKey)
    assert supplier.limit == config.DEFAULT_CONFIG_VARS["interactive_part_matches"]


def test_a_missing_parameter_is_named_not_prompted(
    config_dir: Path, no_terminal: None, offline_digikey: None
):
    with pytest.raises(SupplierLoadError, match="client_secret"):
        create_supplier("digikey", client_id="id", currency="USD", language="en",
                        location="US")


def test_an_unknown_supplier_id_is_an_error(config_dir: Path, no_terminal: None):
    with pytest.raises(SupplierLoadError, match="unknown supplier id"):
        create_supplier("nowhere")


def test_digikey_refuses_a_non_numeric_result_limit(offline_digikey: None):
    """
    `interactive_part_matches` reads like an interactivity flag and is the search `Limit`.
    Passing False for "never ask" used to put `"Limit": false` on the wire and every search
    silently returned nothing.
    """
    with pytest.raises(SupplierLoadError, match="interactive_part_matches"):
        DigiKey().setup(client_id="id", client_secret="s", currency="USD", language="en",
                        location="US", interactive_part_matches=False)


class FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any]):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self):
        from requests.exceptions import HTTPError

        if self.status_code >= 400:
            raise HTTPError(f"{self.status_code}")


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response

    def get(self, url: str, **kwargs: Any):
        return self.response

    def post(self, url: str, **kwargs: Any):
        return self.response


def _api_with(response: FakeResponse) -> DigiKeyApi:
    api = DigiKeyApi.__new__(DigiKeyApi)
    api.session = FakeSession(response)  # pyright: ignore[reportAttributeAccessIssue]
    return api


def test_a_digikey_api_failure_raises_instead_of_returning_nothing():
    api = _api_with(FakeResponse(401, {"detail": "The Client Id is invalid"}))

    with pytest.raises(SupplierError, match="Client Id is invalid") as caught:
        api.keyword_search("anything", limit=5)
    assert "401" in str(caught.value)


def test_a_digikey_not_found_is_still_no_result():
    api = _api_with(FakeResponse(404, {"title": "Not Found"}))

    assert api.product_details("nothing") == {"title": "Not Found"}


class FakeSearchApi:
    """The two calls `DigiKey.search()` makes, answered from a canned keyword result."""

    def __init__(self, keyword_result: dict[str, Any]):
        self.keyword_result = keyword_result

    def product_details(self, product_number: str) -> None:
        return None

    def keyword_search(self, search_term: str, limit: int = 0) -> dict[str, Any]:
        return self.keyword_result


def test_a_digikey_search_that_matches_nothing_is_no_result():
    """
    A keyword search that matches nothing comes back without `ProductsCount` (and without the
    filter options). Reading the count unconditionally turned every part DigiKey does not
    carry into a KeyError, reported as a failed search.
    """
    supplier = DigiKey()
    supplier.limit = 10
    supplier.digikey_api = FakeSearchApi(  # pyright: ignore[reportAttributeAccessIssue]
        {
            "ExactMatches": [],
            "Products": [],
            "SearchLocaleUsed": {"Currency": "USD", "Language": "en", "Site": "US"},
        }
    )

    assert supplier.search("NOT-A-DIGIKEY-PART") == ([], 0)


def test_search_results_are_cached_with_a_bound():
    class Counting(Supplier):
        SUPPORT_LEVEL = SupplierSupportLevel.OFFICIAL_API
        SEARCH_CACHE_SIZE = 2
        calls = 0

        def search(self, search_term: str) -> tuple[list[ApiPart], int]:
            self.calls += 1
            return [], 0

    supplier = Counting()
    supplier.cached_search("a")
    supplier.cached_search("a")
    assert supplier.calls == 1

    supplier.cached_search("b")
    supplier.cached_search("c")          # evicts "a"
    supplier.cached_search("a")
    assert supplier.calls == 4

"""
The LCSC supplier against the shapes its v3 search actually returns.

No network: `LCSCApi` is replaced by a fake that answers from canned dicts and records what
was asked of it. The dicts are trimmed copies of real responses.
"""

from typing import Any

import pytest

from inventree_part_import.exceptions import SupplierError
from inventree_part_import.suppliers import supplier_lcsc
from inventree_part_import.suppliers.supplier_lcsc import LCSC, LCSCApi


def product(code: str, model: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "productCode": code,
        "productModel": model,
        "brandNameEn": "UNI-ROYAL",
        "catalogName": "Chip Resistor - Surface Mount",
        "parentCatalogName": "Resistors",
        "productIntroEn": "5.1kΩ ±1% 62.5mW 0402 Thick Film Resistor",
        "productDescEn": None,
        "encapStandard": "0402",
        "productArrange": "Tape & Reel (TR)",
        "stockNumber": 5668800,
        "productImageUrl": f"https://assets.lcsc.com/images/lcsc/96x96/{code}_front.jpg",
        "productImageUrlBig": f"https://assets.lcsc.com/images/lcsc/900x900/{code}_front.jpg",
        "pdfUrl": f"https://datasheet.lcsc.com/datasheet/pdf/abc.pdf?productCode={code}",
        "url": f"https://www.lcsc.com/product-detail/{code}.html",
        "paramVOList": [{"paramNameEn": "Resistance", "paramValueEn": "5.1kΩ"}],
        "productPriceList": [
            {"ladder": 100, "currencyPrice": 0.0026, "currencySymbol": "$"},
            {"ladder": 1000, "currencyPrice": 0.0018, "currencySymbol": "$"},
        ],
    }
    record.update(overrides)
    return record


def global_result(**overrides: Any) -> dict[str, Any]:
    """The v3 `search/v3/global` result: exact matches listed, the rest only counted."""
    result: dict[str, Any] = {
        "tipProductDetailUrlVO": None,
        "productSearchResultVO": None,
        "exactMatchResult": None,
        "topResults": [],
        "scene": "FULL_MATCH",
        "totalCount": 0,
    }
    result.update(overrides)
    return result


class FakeLCSCApi:
    def __init__(self, currency: str):
        self.currency = currency
        self.global_result: dict[str, Any] | None = None
        self.list_result: list[dict[str, Any]] = []
        self.details: dict[str, dict[str, Any] | None] = {}
        self.calls: list[tuple[str, Any]] = []

    def search(self, keyword: str):
        self.calls.append(("search", keyword))
        return self.global_result

    def product_list(self, keyword: str, page: int = 1):
        self.calls.append(("product_list", keyword))
        return list(self.list_result)

    def product_detail(self, product_code: str):
        self.calls.append(("product_detail", product_code))
        return self.details.get(product_code)


@pytest.fixture
def lcsc(monkeypatch: pytest.MonkeyPatch) -> LCSC:
    monkeypatch.setattr(supplier_lcsc, "LCSCApi", FakeLCSCApi)
    supplier = LCSC()
    supplier.setup(currency="USD")
    return supplier


def fake_api(supplier: LCSC) -> FakeLCSCApi:
    api = supplier.lcsc_api
    assert isinstance(api, FakeLCSCApi)
    return api


def test_a_product_code_is_answered_from_the_detail_page(lcsc: LCSC):
    api = fake_api(lcsc)
    api.global_result = global_result(
        scene="REDIRECT_PRODUCT_DETAIL",
        tipProductDetailUrlVO={"productCode": "C25905", "productModel": "0402WGF5101TCE"},
    )
    api.details["C25905"] = product("C25905", "0402WGF5101TCE")

    parts, count = lcsc.search("C25905")

    assert count == 1 and parts[0].SKU == "C25905" and parts[0].MPN == "0402WGF5101TCE"
    assert ("product_list", "C25905") not in api.calls


def test_an_exact_part_number_match_needs_no_second_request(lcsc: LCSC):
    """The common case -- an MPN -- is answered from the global search alone."""
    api = fake_api(lcsc)
    api.global_result = global_result(
        totalCount=1, exactMatchResult=[product("C25905", "0402WGF5101TCE")]
    )

    parts, count = lcsc.search("0402wgf5101tce")

    assert count == 1 and parts[0].SKU == "C25905"
    assert [name for name, _ in api.calls] == ["search"]


def test_a_partial_part_number_lists_the_products_behind_the_count(lcsc: LCSC):
    """
    `RMCF0402FT10K` matches four Stackpole resistors. The global search only counts them; the
    product list endpoint is where they are, and the prefix filter keeps the noise out.
    """
    api = fake_api(lcsc)
    api.global_result = global_result(totalCount=4, exactMatchResult=None)
    api.list_result = [
        product("C6111655", "RMCF0402FT10K0"),
        product("C4086009", "RMCF0402FT10K7", stockNumber=0),
        product("C99999", "RC0402FR-0710KL"),
    ]

    parts, count = lcsc.search("RMCF0402FT10K")

    assert count == 2
    assert [part.MPN for part in parts] == ["RMCF0402FT10K0", "RMCF0402FT10K7"]
    assert ("product_list", "RMCF0402FT10K") in api.calls


def test_the_stocked_listing_wins_over_its_duplicate(lcsc: LCSC):
    api = fake_api(lcsc)
    api.global_result = global_result(
        totalCount=2,
        exactMatchResult=[
            product("C1", "ABC123", stockNumber=0, productImageUrl=None,
                    productImageUrlBig=None, productImages=None),
            product("C2", "ABC123", stockNumber=500),
        ],
    )

    parts, count = lcsc.search("ABC123")

    assert count == 1 and parts[0].SKU == "C2"


def test_duplicates_are_kept_when_asked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(supplier_lcsc, "LCSCApi", FakeLCSCApi)
    supplier = LCSC()
    supplier.setup(currency="USD", ignore_duplicates=False)
    api = fake_api(supplier)
    api.global_result = global_result(
        totalCount=2,
        exactMatchResult=[
            product("C1", "ABC123", stockNumber=0, productImageUrl=None,
                    productImageUrlBig=None, productImages=None),
            product("C2", "ABC123", stockNumber=500),
        ],
    )

    parts, count = supplier.search("ABC123")

    assert count == 2 and {part.SKU for part in parts} == {"C1", "C2"}


def test_the_older_response_shape_still_works(lcsc: LCSC):
    api = fake_api(lcsc)
    api.global_result = global_result(
        totalCount=1,
        productSearchResultVO={"productList": [product("C8734", "STM32F103C8T6")]},
    )

    parts, count = lcsc.search("STM32F103C8T6")

    assert count == 1 and parts[0].SKU == "C8734"
    assert [name for name, _ in api.calls] == ["search"]


def test_nothing_found_asks_for_no_product_list(lcsc: LCSC):
    api = fake_api(lcsc)
    api.global_result = global_result(totalCount=0)

    assert lcsc.search("NOSUCHPART") == ([], 0)
    assert [name for name, _ in api.calls] == ["search"]


def test_a_part_without_parameters_does_not_crash_when_finalized(lcsc: LCSC):
    """
    Some parts have no parameters at all; the detail page then carries `paramVOList: null`
    rather than an empty list, which used to be a TypeError in the finalize hook.
    """
    api = fake_api(lcsc)
    api.global_result = global_result(
        totalCount=1, exactMatchResult=[product("C7437317", "LCB60-M", paramVOList=None)]
    )
    api.details["C7437317"] = product("C7437317", "LCB60-M", paramVOList=None)

    (part,), _ = lcsc.search("LCB60-M")
    part.finalize_hook()

    assert part.parameters == {"Package Type": "0402"}
    assert ("product_detail", "C7437317") in api.calls


def test_parameters_missing_from_a_listing_are_fetched_from_the_detail_page(lcsc: LCSC):
    api = fake_api(lcsc)
    api.global_result = global_result(
        totalCount=1, exactMatchResult=[product("C1", "ABC", paramVOList=None)]
    )
    api.details["C1"] = product(
        "C1", "ABC", paramVOList=[{"paramNameEn": "Tolerance", "paramValueEn": "±1%"}]
    )

    (part,), _ = lcsc.search("ABC")
    part.finalize_hook()

    assert part.parameters == {"Package Type": "0402", "Tolerance": "±1%"}


def test_the_datasheet_url_is_passed_through_unchanged(lcsc: LCSC):
    """
    datasheet.lcsc.com serves the PDF. The old rewrite onto wmsc.lcsc.com now returns an
    HTML page that redirects to the product, which InvenTree then stored as the datasheet.
    """
    api = fake_api(lcsc)
    api.global_result = global_result(totalCount=1, exactMatchResult=[product("C1", "ABC")])

    (part,), _ = lcsc.search("ABC")

    assert part.datasheet_url == "https://datasheet.lcsc.com/datasheet/pdf/abc.pdf?productCode=C1"


def test_a_part_without_prices_falls_back_to_the_configured_currency(lcsc: LCSC):
    api = fake_api(lcsc)
    api.global_result = global_result(
        totalCount=1, exactMatchResult=[product("C1", "ABC", productPriceList=[])]
    )

    (part,), _ = lcsc.search("ABC")

    assert part.price_breaks == {} and part.currency == "USD"


def test_the_currency_comes_from_the_price_list(lcsc: LCSC):
    api = fake_api(lcsc)
    api.global_result = global_result(
        totalCount=1,
        exactMatchResult=[product(
            "C1", "ABC",
            productPriceList=[{"ladder": 1, "currencyPrice": 0.5, "currencySymbol": "€"}],
        )],
    )

    (part,), _ = lcsc.search("ABC")

    assert part.currency == "EUR" and part.price_breaks == {1: 0.5}


# ─── The HTTP layer ───────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, body: dict[str, Any], status_code: int = 200):
        self.body = body
        self.status_code = status_code
        self.content = b"x"

    def json(self) -> dict[str, Any]:
        return self.body


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def get(self, url: str) -> FakeResponse:
        self.requests.append(("GET", url, None))
        return self.response

    def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
        self.requests.append(("POST", url, json))
        return self.response


def bare_api(response: FakeResponse) -> tuple[LCSCApi, FakeSession]:
    api = LCSCApi.__new__(LCSCApi)
    session = FakeSession(response)
    api.session = session  # pyright: ignore[reportAttributeAccessIssue]
    return api, session


def test_a_failure_reported_inside_a_200_body_is_an_error():
    """The product list endpoint answers a bad request with HTTP 200 and its own code."""
    api, _ = bare_api(FakeResponse({"code": 405, "msg": "Invalid field.", "result": None}))

    with pytest.raises(SupplierError, match="Invalid field"):
        api.product_list("anything")


def test_the_product_list_sends_the_search_term_in_the_clear_and_pages():
    api, session = bare_api(FakeResponse({"code": 200, "result": {"dataList": [{"productCode": "C1"}]}}))

    products = api.product_list("RMCF0402FT10K", page=2)

    assert products == [{"productCode": "C1"}]
    method, url, body = session.requests[0]
    assert method == "POST" and url == LCSCApi.PRODUCT_LIST_URL
    assert body == {"keyword": "RMCF0402FT10K", "currentPage": 2,
                    "pageSize": LCSCApi.PRODUCT_LIST_PAGE_SIZE}


def test_an_empty_product_list_is_an_empty_list():
    api, _ = bare_api(FakeResponse({"code": 200, "result": {"dataList": None}}))
    assert api.product_list("x") == []

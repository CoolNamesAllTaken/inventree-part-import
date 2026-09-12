import base64
import re
from types import MethodType
from typing import Any

from fake_useragent import UserAgent
from gmssl.sm2 import CryptSM2
from requests.compat import quote
from requests.exceptions import JSONDecodeError

from .. import retries
from ..exceptions import SupplierError
from .base import REMOVE_HTML_TAGS, ApiPart, Supplier, SupplierSupportLevel


class LCSC(Supplier):
    SUPPORT_LEVEL = SupplierSupportLevel.INOFFICIAL_API

    def setup(self, *, currency: str, ignore_duplicates: bool = True, **kwargs: Any):
        if currency not in CURRENCY_MAP.values():
            self.load_error(f"unsupported currency '{currency}'")

        self.currency = currency
        self.ignore_duplicates = ignore_duplicates

        self.lcsc_api = LCSCApi(self.currency)

    def search(self, search_term: str) -> tuple[list[ApiPart], int]:
        if not (result := self.lcsc_api.search(search_term)):
            return [], 0

        # A product code (C25905) is answered with a redirect to the product's detail page.
        if product_detail := result.get("tipProductDetailUrlVO"):
            if detail_result := self.lcsc_api.product_detail(product_detail["productCode"]):
                return [self.get_api_part(detail_result)], 1
            return [], 0

        term = search_term.lower()

        def is_exact(product: dict[str, Any]) -> bool:
            return (
                (product.get("productModel") or "").lower() == term
                or (product.get("productCode") or "").lower() == term
            )

        def is_match(product: dict[str, Any]) -> bool:
            return (
                (product.get("productModel") or "").lower().startswith(term)
                or (product.get("productCode") or "").lower() == term
            )

        # The v3 global search lists only the products whose part number IS the search term
        # (`exactMatchResult`); everything else it merely counts. The older response shape
        # carried the whole page in `productSearchResultVO`, so keep reading that too.
        products: list[dict[str, Any]] = []
        if search_result := result.get("productSearchResultVO"):
            products.extend(search_result.get("productList") or [])
        products.extend(result.get("exactMatchResult") or [])
        products = _unique_by_code(products)

        exact_matches = self._prefer_stocked([p for p in products if is_exact(p)])
        if len(exact_matches) == 1:
            return [self.get_api_part(exact_matches[0])], 1

        # Anything beyond the exact matches lives behind the product list endpoint, which
        # takes the search term in the clear and pages.
        total_count = result.get("totalCount")
        if total_count is None or total_count > len(products):
            products = _unique_by_code(products + self.lcsc_api.product_list(search_term))

        filtered_matches = [product for product in products if is_match(product)]
        exact_matches = self._prefer_stocked([p for p in filtered_matches if is_exact(p)])
        if len(exact_matches) == 1:
            return [self.get_api_part(exact_matches[0])], 1

        return list(map(self.get_api_part, filtered_matches)), len(filtered_matches)

    def _prefer_stocked(self, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        LCSC lists some parts twice, and the duplicate is the one with no stock and no
        picture. With `ignore_duplicates` those are dropped when a better listing exists.
        """
        if not self.ignore_duplicates:
            return products
        stocked = [
            product
            for product in products
            if product.get("stockNumber")
            or product.get("productImageUrlBig")
            or product.get("productImageUrl")
            or product.get("productImages")
        ]
        return stocked if stocked else products

    def get_api_part(self, lcsc_part: dict[str, Any]):
        if not (description := lcsc_part.get("productDescEn")):
            description = lcsc_part.get("productIntroEn")
        description = description.strip() if description else ""

        image_url = lcsc_part.get("productImageUrlBig", lcsc_part.get("productImageUrl"))
        if not image_url and (image_urls := lcsc_part.get("productImages")):
            for image_url in reversed(image_urls):
                if "front" in image_url:
                    break

        # `pdfUrl` (on datasheet.lcsc.com) serves the PDF as given. It used to be rewritten
        # onto wmsc.lcsc.com, which now answers with an HTML redirect to the product page.
        datasheet_url = lcsc_part.get("pdfUrl") or None

        if url := lcsc_part.get("url"):
            url_separator = "/product-detail/"
            prefix, product_url_id = url.split(url_separator)
            product_url_id = product_url_id
            supplier_link = url_separator.join((prefix, cleanup_url_id(product_url_id)))
        else:
            product_url_id = cleanup_url_id(
                "_".join((lcsc_part["catalogName"], lcsc_part["title"], lcsc_part["productCode"]))
            )
            supplier_link = f"https://www.lcsc.com/product-detail/{product_url_id}.html"

        product_arrange = lcsc_part.get("productArrange")
        packaging = REMOVE_HTML_TAGS.sub("", product_arrange) if product_arrange else ""

        category_path: list[str] = []
        if parent := lcsc_part.get("parentCatalogName"):
            category_path.append(parent)
        if category := lcsc_part.get("catalogName"):
            category_path.append(category)

        finalize = False
        parameters = {}
        if lcsc_parameters := lcsc_part.get("paramVOList"):
            parameters = {
                parameter.get("paramNameEn"): parameter.get("paramValueEn")
                for parameter in lcsc_parameters
            }
        else:
            finalize = True

        if package := lcsc_part.get("encapStandard"):
            parameters["Package Type"] = package

        price_list: Any = lcsc_part["productPriceList"] or []
        price_breaks = {
            price_break.get("ladder"): price_break.get("currencyPrice")
            for price_break in price_list
        }
        currency_symbol: str | None = price_list[0].get("currencySymbol") if price_list else None
        currency = (CURRENCY_MAP.get(currency_symbol) if currency_symbol else None) or self.currency

        api_part = ApiPart(
            description=REMOVE_HTML_TAGS.sub("", description),
            image_url=image_url,
            datasheet_url=datasheet_url,
            supplier_link=supplier_link,
            SKU=lcsc_part["productCode"],
            manufacturer=REMOVE_HTML_TAGS.sub("", lcsc_part.get("brandNameEn") or ""),
            manufacturer_link="",
            MPN=lcsc_part.get("productModel") or "",
            quantity_available=float(lcsc_part.get("stockNumber") or 0),
            packaging=packaging,
            category_path=category_path,
            parameters=parameters,
            price_breaks=price_breaks,
            currency=currency,
        )

        if finalize:
            api_part.finalize_hook = MethodType(self.finalize_hook, api_part)

        return api_part

    def finalize_hook(self, api_part: ApiPart):
        # Search results carry no parameters for some parts; the detail page usually does.
        # Some parts genuinely have none, and `paramVOList` is then null rather than empty.
        detail: dict[str, Any] = self.lcsc_api.product_detail(api_part.SKU) or {}
        parameters: Any = detail.get("paramVOList") or []
        api_part.parameters |= {
            parameter.get("paramNameEn"): parameter.get("paramValueEn")
            for parameter in parameters
        }


def _unique_by_code(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for product in products:
        code = product.get("productCode") or ""
        if code in seen:
            continue
        seen.add(code)
        unique.append(product)
    return unique


class LCSCApi:
    MAIN_PAGE_URL = "https://www.lcsc.com/"
    API_BASE_URL = "https://wmsc.lcsc.com/ftps/wm/"
    SEARCH_URL = f"{API_BASE_URL}search/v3/global"
    PRODUCT_LIST_URL = f"{API_BASE_URL}product/query/list"
    PRODUCT_INFO_URL = f"{API_BASE_URL}product/detail?productCode={{}}"
    CURRENCY_URL = "https://wmsc.lcsc.com/wmsc/home/currency?currencyCode={}"

    #: One page of the product list is all a part number search needs: the site itself
    #: shows 25 and pages from there.
    PRODUCT_LIST_PAGE_SIZE = 50

    def __init__(self, currency: str):
        self.session = retries.setup_session()
        self.session.headers.update(
            {"User-Agent": UserAgent(os=["iOS"]).random, "Accept-Language": "en-US,en"}
        )
        self.session.get(self.CURRENCY_URL.format(currency))

        response = self.session.get(self.MAIN_PAGE_URL)
        if not (public_key_match := re.search(r'encryptPublicHexKey:"([a-f0-9]+)"', response.text)):
            raise SupplierError(
                "LCSC", f"Failed to find 'encryptPublicHexKey' in {self.MAIN_PAGE_URL} page content"
            )
        self.sm2 = CryptSM2(None, public_key_match.group(1), mode=1)

    def search(self, keyword: str):
        """
        The global search: which product a code redirects to, the exact part number matches,
        and a count of everything else.
        """
        assert (keyword_encrypted := self.sm2.encrypt(base64.b64encode(keyword.encode("utf-8"))))
        return self._api_call(
            self.SEARCH_URL, json={"keyword": f"{{secret}}04{keyword_encrypted.hex()}"}
        )

    def product_list(self, keyword: str, page: int = 1) -> list[dict[str, Any]]:
        """The product records behind a search, as the site's results table lists them."""
        result: dict[str, Any] = self._api_call(
            self.PRODUCT_LIST_URL,
            json={"keyword": keyword, "currentPage": page, "pageSize": self.PRODUCT_LIST_PAGE_SIZE},
        ) or {}
        products: list[dict[str, Any]] = result.get("dataList") or []
        return products

    def product_detail(self, product_code: str):
        return self._api_call(self.PRODUCT_INFO_URL.format(quote(product_code, safe="")))

    def _api_call(self, url: str, json: dict[str, Any] | None = None) -> Any:
        result = self.session.get(url) if json is None else self.session.post(url, json=json)

        if not result.content:
            raise SupplierError(
                "LCSC", f"Request failed with code {result.status_code} (no content)"
            )

        try:
            content_json: dict[str, Any] = result.json()
        except JSONDecodeError as e:
            raise SupplierError("LCSC", str(e))

        # Failures come back as HTTP 200 with their own code in the body ("Invalid field",
        # code 405, say), so the body's code is the one that counts.
        code = content_json.get("code")
        if result.status_code != 200 or (code is not None and code != 200):
            message = content_json.get("msg") or f"Request failed with code {code or result.status_code}"
            raise SupplierError("LCSC", message)

        return content_json["result"]


CLEANUP_URL_ID_REGEX = re.compile(r"[^\w\d\.]")


def cleanup_url_id(url: str):
    url = url.replace(" / ", "_")
    url = CLEANUP_URL_ID_REGEX.sub("_", url)
    return url


CURRENCY_MAP = {
    "$": "USD",
    "€": "EUR",
    "¥": "CNY",
    "HK$": "HKD",
}

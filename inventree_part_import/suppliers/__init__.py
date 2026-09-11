import importlib
from inspect import isclass
from multiprocessing.pool import ThreadPool
from pathlib import Path
from typing import Any, Mapping

from error_helper import error, hint
from inventree.api import InvenTreeAPI
from inventree.company import Company as InvenTreeCompany

from ..config import SUPPLIERS_CONFIG, get_config, load_suppliers_config, update_config_file
from ..exceptions import ConfigurationError, SupplierLoadError
from ..inventree_helpers import Company
from .base import ScrapeSupplier, Supplier

_suppliers = None


def search(search_term: str, supplier_id: str | None = None, only_supplier: bool = False):
    global _suppliers
    if _suppliers is None:
        assert _supplier_companies is not None, "call setup_supplier_companies(...) first"
        supplier_objects, _ = get_suppliers()
        assert supplier_objects.keys() == _supplier_companies.keys()
        _suppliers = dict(
            zip(
                supplier_objects.keys(),
                zip(supplier_objects.values(), _supplier_companies.values()),
            )
        )

    suppliers = list(_suppliers.values())
    if supplier_id:
        if supplier := _suppliers.get(supplier_id):
            if only_supplier:
                suppliers = [supplier]
            else:
                suppliers.remove(supplier)
                suppliers.insert(0, supplier)
        else:
            error(f"supplier id '{supplier_id}' not defined in {SUPPLIERS_CONFIG}")
            return None

    thread_pool = ThreadPool(processes=8)
    return (
        (api_company, thread_pool.apply_async(supplier_object.cached_search, (search_term,)))
        for supplier_object, api_company in suppliers
    )


_supplier_companies: dict[str, InvenTreeCompany] | None = None


def setup_supplier_companies(inventree_api: InvenTreeAPI):
    global _supplier_companies
    _supplier_companies = {}
    global_config = get_config()

    with update_config_file(SUPPLIERS_CONFIG) as suppliers_config:
        assert _supplier_objects is not None
        for id, supplier_object in _supplier_objects.items():
            supplier_config: dict[str, Any] | None = suppliers_config.get(id)
            if supplier_config is None:
                supplier_config = suppliers_config[id] = {}
            api_company = Company(
                name=supplier_object.name,
                currency=supplier_config.get("currency", global_config["currency"]),
                is_supplier=True,
                primary_key=supplier_config.get("_primary_key"),
            ).setup(inventree_api)
            if not hasattr(inventree_api, "DRY_RUN"):
                supplier_config["_primary_key"] = api_company.pk
            _supplier_companies[id] = api_company


_supplier_objects: dict[str, Supplier] | None = None
_available_supplier_objects: dict[str, Supplier] | None = None


def discover_supplier_classes() -> dict[str, type[Supplier]]:
    """
    Every supplier module in this package, keyed by its id (`supplier_<id>.py`).

    A module that fails to import or defines no single `Supplier` subclass is reported and
    left out; one broken supplier must not remove the others.
    """
    classes: dict[str, type[Supplier]] = {}
    for path in Path(__file__).parent.glob("supplier_*.py"):
        module_name = path.stem
        try:
            module = importlib.import_module(f".{module_name}", package=__package__)
        except ImportError as e:
            error(f"failed to load supplier module '{module_name}' with {e}")
            continue

        supplier_classes = [
            cls
            for cls in vars(module).values()
            if isclass(cls) and cls not in (Supplier, ScrapeSupplier) and issubclass(cls, Supplier)
        ]
        if len(supplier_classes) != 1:
            suffix = "multiple Supplier classes" if supplier_classes else "no Supplier class"
            error(f"failed to load supplier module '{module_name}' ({suffix} defined)")
            continue

        if not hasattr(supplier_classes[0], "SUPPORT_LEVEL"):
            error(f"failed to load supplier module '{module_name}' (undefined SUPPORT_LEVEL)")
            continue

        id = module_name.split("supplier_", 1)[-1]
        classes[id] = supplier_classes[0]

    return classes


def available_supplier_ids() -> list[str]:
    return sorted(discover_supplier_classes())


def create_supplier(id: str, **parameters: Any) -> Supplier:
    """
    Build one configured supplier from explicit parameters.

    For programs that embed this package: nothing here reads or writes `suppliers.yaml`, and
    nothing prompts. `parameters` are the keyword arguments of the supplier's `setup()` --
    `client_id`, `currency`, and so on -- and a required one that is missing is a
    `SupplierLoadError` naming it, not a question on stdin. A parameter that is not given
    falls back to the global configuration (see `config.set_config()`), which is what the
    CLI's `suppliers.yaml` does too.

    Search with `supplier.search(term)`; it raises `SupplierError` when the supplier's API
    fails, which is not the same thing as finding nothing.
    """
    classes = discover_supplier_classes()
    if not (cls := classes.get(id)):
        raise SupplierLoadError(
            id, f"unknown supplier id (available: {', '.join(sorted(classes)) or 'none'})"
        )

    supplier = cls()
    try:
        global_config: Mapping[str, Any] = get_config()
    except ConfigurationError:
        global_config = {}

    setup_parameters: dict[str, Any] = {}
    missing: list[str] = []
    for name, default in supplier.get_setup_params().items():
        if name in parameters:
            setup_parameters[name] = parameters[name]
        elif default is not None:
            setup_parameters[name] = default
        elif (value := global_config.get(name)) is not None:
            setup_parameters[name] = value
        else:
            missing.append(name)
    if missing:
        raise SupplierLoadError(
            supplier.name, f"missing setup parameter(s): {', '.join(missing)}"
        )

    extra = {name: value for name, value in parameters.items() if name not in setup_parameters}
    try:
        supplier.setup(**setup_parameters, **extra)
    except TypeError as e:
        raise SupplierLoadError(supplier.name, f"invalid setup parameters ({e})") from e
    return supplier


def get_suppliers(reload: bool = False, setup: bool = True):
    global _supplier_objects, _available_supplier_objects
    if not reload and _supplier_objects is not None and _available_supplier_objects is not None:
        return _supplier_objects, _available_supplier_objects

    _supplier_objects = {}
    _available_supplier_objects = {
        id: cls() for id, cls in discover_supplier_classes().items()
    }

    _available_supplier_objects = dict(
        sorted(
            _available_supplier_objects.items(),
            key=lambda supplier_item: (supplier_item[1].SUPPORT_LEVEL, supplier_item[1].name),
        )
    )

    _supplier_objects = load_suppliers_config(_available_supplier_objects, setup=setup)

    if (available := len(_available_supplier_objects)) > (loaded := len(_supplier_objects)):
        if setup:
            hint(f"only {loaded} of {available} available supplier modules are configured")

    return _supplier_objects, _available_supplier_objects

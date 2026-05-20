from .base import Adapter, PageHint, CaptureResult, PageIdentity, AdapterError

__all__ = ["Adapter", "PageHint", "CaptureResult", "PageIdentity", "AdapterError"]


def get_adapter(name: str) -> Adapter:
    if name == "generic_android":
        from .android import AndroidAdapter
        return AndroidAdapter()
    if name == "generic_ios":
        from .ios import IOSAdapter
        return IOSAdapter()
    if name == "generic_harmony":
        from .harmony import HarmonyAdapter
        return HarmonyAdapter()
    raise AdapterError(
        f"No UI adapter for adapter.name={name!r}. "
        "Implement adapters/<name>.py and register it in adapters/__init__.py."
    )

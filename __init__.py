import logging

NODE_CLASS_MAPPINGS: dict = {}
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]

log = logging.getLogger("comfyui-model-crud")

# NFR-5: a bug in this extension must never stop ComfyUI from starting. Importing api.register
# registers the routes as a side effect; if that fails we log and stay inert.
try:
    from .api import register  # noqa: F401
except Exception:
    log.exception("comfyui-model-crud failed to load and is disabled")

import logging

NODE_CLASS_MAPPINGS: dict = {}
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]

log = logging.getLogger("comfyui-model-crud")

# NFR-5: a bug in this extension must never stop ComfyUI from starting. Importing the api
# package registers the routes as a side effect; if that fails we log and stay inert.
try:
    from . import api  # noqa: F401
except Exception:
    log.exception("comfyui-model-crud failed to load and is disabled")

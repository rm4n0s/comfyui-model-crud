"""Importing this module registers our routes on ComfyUI's server (and logs exposure warnings).

Kept apart from ``routes`` so the handlers can be imported and tested without ComfyUI.
"""

import logging

from server import PromptServer

from ..services.config import get_config
from ..services.exposure import warn_if_exposed
from .routes import register_lifecycle, register_routes

register_routes(PromptServer.instance.routes)

_app = getattr(PromptServer.instance, "app", None)
if _app is not None:
    register_lifecycle(_app)
else:  # ComfyUI changed shape: uploads still work, they just will not survive a restart
    logging.getLogger("comfyui-model-crud").warning(
        "PromptServer.app not found; unfinished uploads will not be resumed after a restart."
    )

try:
    warn_if_exposed(get_config())
except Exception:  # a warning helper must never disable the routes registered above
    logging.getLogger("comfyui-model-crud").exception("Could not check server exposure")

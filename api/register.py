"""Importing this module registers our routes on ComfyUI's server (and logs exposure warnings).

Kept apart from ``routes`` so the handlers can be imported and tested without ComfyUI.
"""

import logging

from server import PromptServer

from ..services.config import get_config
from ..services.exposure import warn_if_exposed
from .routes import register_routes

register_routes(PromptServer.instance.routes)

try:
    warn_if_exposed(get_config())
except Exception:  # a warning helper must never disable the routes registered above
    logging.getLogger("comfyui-model-crud").exception("Could not check server exposure")

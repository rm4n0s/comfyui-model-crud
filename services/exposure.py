"""Startup warnings about running with writes enabled on an exposed server (SR-10)."""

import ipaddress
import logging

from . import comfy
from .config import Config

log = logging.getLogger("comfyui-model-crud")


def _is_loopback(address: str) -> bool:
    if address == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False  # any other hostname: assume it is reachable from elsewhere


def warn_if_exposed(config: Config) -> None:
    if not (config.upload_enabled or config.delete_enabled):
        return
    addresses = comfy.listen_addresses()
    if addresses and not all(_is_loopback(a) for a in addresses):
        log.warning(
            "ComfyUI is listening on %s and has no authentication, so anyone who can reach it "
            "can upload and delete model files. Set MODEL_CRUD_READ_ONLY=1 to disable writes.",
            ", ".join(addresses),
        )
    if comfy.cors_enabled():
        log.warning(
            "--enable-cors-header replaces ComfyUI's origin check, so web pages on other sites "
            "may be able to upload and delete model files through this browser's session. "
            "Set MODEL_CRUD_READ_ONLY=1 to disable writes."
        )

"""Typed errors carrying the HTTP status and machine code from SPEC §6.

Services raise these; handlers turn them into ``{"error": code, "message": text}`` responses.
Messages are written for clients, so they must never contain absolute server paths (SR-3).
"""

from typing import Any


class ModelCrudError(Exception):
    status: int = 500
    code: str = "internal_error"
    default_message: str = "Internal error."

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.message = message or self.default_message
        # Extra machine-readable fields merged into the body, e.g. ``missing_ranges``.
        self.details = details
        super().__init__(self.message)

    def to_body(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.details}


class BadRequest(ModelCrudError):
    status = 400
    code = "bad_request"
    default_message = "Malformed request."


class InvalidFilename(ModelCrudError):
    status = 400
    code = "invalid_filename"
    default_message = "Invalid file name."


class PathOutsideRoot(ModelCrudError):
    status = 400
    code = "path_outside_root"
    default_message = "Path escapes the model folder."


class BadChunk(ModelCrudError):
    status = 400
    code = "bad_chunk"
    default_message = "Invalid chunk index or length."


class UploadsDisabled(ModelCrudError):
    status = 403
    code = "uploads_disabled"
    default_message = "Uploads are disabled on this server."


class DeleteDisabled(ModelCrudError):
    status = 403
    code = "delete_disabled"
    default_message = "Deleting models is disabled on this server."


class UnknownFolder(ModelCrudError):
    status = 404
    code = "unknown_folder"
    default_message = "Unknown model folder."


class ModelFileNotFound(ModelCrudError):
    status = 404
    code = "file_not_found"
    default_message = "File not found."


class UnknownUpload(ModelCrudError):
    status = 404
    code = "unknown_upload"
    default_message = "Unknown upload."


class FileAlreadyExists(ModelCrudError):
    status = 409
    code = "file_exists"
    default_message = "A file with this name already exists."


class UploadInProgress(ModelCrudError):
    status = 409
    code = "upload_in_progress"
    default_message = "Another upload to this file is already in progress."


class IncompleteUpload(ModelCrudError):
    status = 409
    code = "incomplete_upload"
    default_message = "Not all chunks have been received."


class TooLarge(ModelCrudError):
    status = 413
    code = "too_large"
    default_message = "File is larger than the configured upload limit."


class ExtensionNotAllowed(ModelCrudError):
    status = 415
    code = "extension_not_allowed"
    default_message = "This file extension is not allowed."


class ChecksumMismatch(ModelCrudError):
    status = 422
    code = "checksum_mismatch"
    default_message = "Checksum does not match."


class TooManyUploads(ModelCrudError):
    status = 429
    code = "too_many_uploads"
    default_message = "Too many uploads are in progress."


class InsufficientStorage(ModelCrudError):
    status = 507
    code = "insufficient_storage"
    default_message = "Not enough free disk space."

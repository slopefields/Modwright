"""Error types carrying stable, machine-checkable codes.

Every tool failure returns a `code` from `ErrorCode` alongside its human-readable
message, so a calling agent can branch programmatically instead of matching on
error strings. Modelled on DecompilerServer's own fixed error enum.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    #: The install root does not match any registered framework adapter.
    UNSUPPORTED_GAME = "unsupported_game"
    #: The game is IL2CPP, so its real logic cannot be decompiled by anything
    #: built on an IL decompiler. Refused explicitly rather than half-working.
    IL2CPP_UNSUPPORTED = "il2cpp_unsupported"
    #: Path given as an install root does not exist or is not a directory.
    INVALID_INSTALL_ROOT = "invalid_install_root"
    #: No mod project (or no ModWright config) at the given path.
    PROJECT_NOT_FOUND = "project_not_found"
    #: Scaffolding would overwrite a project that is already there.
    PROJECT_EXISTS = "project_exists"
    #: Mod name is not usable as a C# assembly name and namespace.
    INVALID_MOD_NAME = "invalid_mod_name"
    #: `dotnet build`/`publish` returned non-zero.
    BUILD_FAILED = "build_failed"
    #: Destination file is locked, which almost always means the game is running.
    ARTIFACT_LOCKED = "artifact_locked"
    #: Deploy target is not a usable loader tree for this framework.
    INVALID_DEPLOY_ROOT = "invalid_deploy_root"
    #: More than one place this mod could be installed, and none was chosen.
    #: Not a failure to be retried -- the user has to pick.
    DEPLOY_TARGET_UNSET = "deploy_target_unset"
    #: A referenced mod is not installed where the project deploys.
    MOD_REFERENCE_NOT_FOUND = "mod_reference_not_found"
    #: The framework's log file does not exist yet (game never run since install).
    LOG_NOT_FOUND = "log_not_found"
    #: DecompilerServer could not be reached or returned an error.
    DECOMPILER_UNAVAILABLE = "decompiler_unavailable"
    #: The adapter exists but has not implemented this step yet.
    NOT_IMPLEMENTED = "not_implemented"


class ModwrightError(Exception):
    """Base for all errors that map onto a stable tool-response code."""

    code: ErrorCode = ErrorCode.NOT_IMPLEMENTED

    def __init__(
        self,
        message: str,
        *,
        hints: list[str] | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hints = hints or []
        #: Structured data the agent needs to act on the failure -- the list of
        #: profiles to choose between, say. Hints are prose; this is not.
        self.details = details or {}

    def to_response(self) -> dict:
        payload: dict = {"success": False, "code": str(self.code), "error": self.message}
        if self.hints:
            payload["hints"] = self.hints
        payload.update(self.details)
        return payload


class UnsupportedGameError(ModwrightError):
    code = ErrorCode.UNSUPPORTED_GAME


class Il2CppUnsupportedError(ModwrightError):
    code = ErrorCode.IL2CPP_UNSUPPORTED


class InvalidInstallRootError(ModwrightError):
    code = ErrorCode.INVALID_INSTALL_ROOT


class ProjectNotFoundError(ModwrightError):
    code = ErrorCode.PROJECT_NOT_FOUND


class ProjectExistsError(ModwrightError):
    code = ErrorCode.PROJECT_EXISTS


class InvalidModNameError(ModwrightError):
    code = ErrorCode.INVALID_MOD_NAME


class BuildFailedError(ModwrightError):
    code = ErrorCode.BUILD_FAILED


class ArtifactLockedError(ModwrightError):
    code = ErrorCode.ARTIFACT_LOCKED


class InvalidDeployRootError(ModwrightError):
    code = ErrorCode.INVALID_DEPLOY_ROOT


class DeployTargetUnsetError(ModwrightError):
    code = ErrorCode.DEPLOY_TARGET_UNSET


class ModReferenceNotFoundError(ModwrightError):
    code = ErrorCode.MOD_REFERENCE_NOT_FOUND


class LogNotFoundError(ModwrightError):
    code = ErrorCode.LOG_NOT_FOUND


class DecompilerUnavailableError(ModwrightError):
    code = ErrorCode.DECOMPILER_UNAVAILABLE


class AdapterStepNotImplementedError(ModwrightError):
    code = ErrorCode.NOT_IMPLEMENTED

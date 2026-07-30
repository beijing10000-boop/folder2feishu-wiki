from __future__ import annotations


class FeishuError(RuntimeError):
    """Base error that never embeds access or refresh tokens."""


class FeishuProtocolError(FeishuError):
    pass


class FeishuTransportError(FeishuError):
    pass


class FeishuAmbiguousWriteError(FeishuTransportError):
    """A write may have reached Feishu, so callers must reconcile before retrying."""


class WikiMoveTaskFailedError(FeishuError):
    """A persisted move-to-Wiki task reached an explicit terminal failure."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class FeishuAPIError(FeishuError):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        status_code: int | None = None,
        log_id: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.log_id = log_id
        self.retryable = retryable


class OAuthError(FeishuError):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class OAuthStateError(OAuthError):
    pass


class MissingScopesError(OAuthError):
    def __init__(self, missing_scopes: set[str]) -> None:
        self.missing_scopes = frozenset(missing_scopes)
        super().__init__(
            "OAuth authorization is missing required scopes: "
            + ", ".join(sorted(self.missing_scopes))
        )


class UploadSessionError(FeishuError):
    pass


class ReconciliationError(FeishuError):
    pass

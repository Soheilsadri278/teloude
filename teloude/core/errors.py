# teloude/core/errors.py
"""Classifying transfer failures: network trouble vs local filesystem trouble.

Telegram call failures reach the engines as Teloude's own exception types
(the gateways map them), and local file problems reach them as OSError from
open()/read()/write(). The two need very different reactions: a network drop is
retried, while a permission or disk-space problem will never fix itself and the
user must be told what is actually wrong - "check your internet connection" for
a read-only folder is worse than useless.
"""
import errno
from typing import Optional

# OSError errnos that really mean "the network is unhappy".
NETWORK_ERRNOS = frozenset(
    {
        errno.ENETUNREACH,
        errno.ENETDOWN,
        errno.ENETRESET,
        errno.EHOSTUNREACH,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.ETIMEDOUT,
        errno.EPIPE,
    }
)

# Local conditions worth naming explicitly, because the default text is vague.
_LOCAL_MESSAGES = {
    errno.EACCES: "permission denied",
    errno.EPERM: "permission denied",
    errno.ENOSPC: "no space left on this drive",
    errno.EROFS: "this drive is read-only",
    errno.EMFILE: "too many files are open",
    errno.ENOENT: "the file no longer exists",
    errno.EISDIR: "the path is a folder, not a file",
    errno.ENOTDIR: "part of the path is a file, not a folder",
    errno.ENAMETOOLONG: "the path is too long for this filesystem",
    errno.EEXIST: "the destination already exists",
    errno.EBUSY: "the file is in use by another program",
}


def error_number(exc: BaseException) -> Optional[int]:
    """Best-effort errno of an OSError-like exception."""
    number = getattr(exc, "errno", None)
    return number if isinstance(number, int) else None


def is_network_error(exc: BaseException) -> bool:
    """True when retrying later could plausibly succeed."""
    from teloude.infrastructure.telegram.exceptions import ConnectionStateError

    if isinstance(exc, ConnectionStateError):
        return True  # the gateway already decided Telegram is unreachable
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    number = error_number(exc)
    if number in NETWORK_ERRNOS:
        return True
    # A bare OSError from a socket layer carries no errno we recognize; treat
    # only real OSErrors without a local errno as network-ish.
    return isinstance(exc, OSError) and number is None


def local_failure_message(exc: BaseException, subject: str = "") -> str:
    """Human text for a local filesystem failure (never mentions the network)."""
    number = error_number(exc)
    what = _LOCAL_MESSAGES.get(number)
    if what is None:
        what = str(exc) or type(exc).__name__
    if subject:
        return f"{subject}: {what}."
    return what[0].upper() + what[1:] + "."

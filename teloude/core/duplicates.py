# teloude/core/duplicates.py
"""Duplicate detection: cheap fingerprint first, SHA-256 to confirm.

Two files are duplicates only when full SHA-256 matches. Nothing is ever
deleted or overwritten automatically; every match produces an explicit
DuplicateMatch for the caller (UI) to resolve via DuplicateAction.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from .scanner import ScannedFile


class DuplicateAction(Enum):
    SKIP = "skip"              # do not upload this file
    UPLOAD_AGAIN = "upload"    # upload even though content exists remotely
    CANCEL = "cancel"          # abort the whole backup operation


@dataclass
class DuplicateMatch:
    scanned: ScannedFile
    existing_storage: str      # storage holding the identical content
    existing_path: str         # relative path of the identical file


@dataclass
class DuplicateDecision:
    action: DuplicateAction
    apply_to_all: bool = False


class DuplicateResolver:
    """Applies a duplicate policy; asks the user only when policy is ASK.

    ask_callback receives the DuplicateMatch plus 1-based (index, total) and
    returns a DuplicateDecision. apply_to_all answers are remembered for the
    rest of the operation only (never persisted).
    """

    ASK = "ask"
    SKIP_ALL = "skip_all"
    UPLOAD_ALL = "upload_all"

    def __init__(
        self,
        policy: str = ASK,
        ask_callback: Optional[Callable[[DuplicateMatch, int, int], DuplicateDecision]] = None,
    ):
        if policy not in (self.ASK, self.SKIP_ALL, self.UPLOAD_ALL):
            raise ValueError(f"Unknown duplicate policy: {policy}")
        if policy == self.ASK and ask_callback is None:
            raise ValueError("ASK policy requires an ask_callback.")
        self._policy = policy
        self._ask = ask_callback
        self._remembered: Optional[DuplicateAction] = None

    @property
    def policy(self) -> str:
        return self._policy

    def decide(self, match: DuplicateMatch, index: int, total: int) -> DuplicateAction:
        if self._policy == self.SKIP_ALL:
            return DuplicateAction.SKIP
        if self._policy == self.UPLOAD_ALL:
            return DuplicateAction.UPLOAD_AGAIN
        if self._remembered is not None:
            return self._remembered
        assert self._ask is not None
        decision = self._ask(match, index, total)
        if decision.apply_to_all and decision.action is not DuplicateAction.CANCEL:
            self._remembered = decision.action
        return decision.action


def find_duplicates(
    scanned: List[ScannedFile],
    lookup_sha: Callable[[str], List[Dict[str, str]]],
) -> List[DuplicateMatch]:
    """Returns matches for scanned files whose SHA-256 already exists remotely.

    lookup_sha(sha256) returns known records as dicts with at least
    'storage' and 'relative_path'. Files without a computed sha256 are
    skipped (caller must hash first); at most one match per file is reported.
    """
    matches: List[DuplicateMatch] = []
    for item in scanned:
        if not item.sha256:
            continue
        for known in lookup_sha(item.sha256):
            matches.append(
                DuplicateMatch(
                    scanned=item,
                    existing_storage=known.get("storage", "?"),
                    existing_path=known.get("relative_path", "?"),
                )
            )
            break
    return matches

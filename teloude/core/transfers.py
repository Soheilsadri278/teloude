# teloude/core/transfers.py
"""Transfer state machine with persistent checkpoints.

Every state change is written through TransferRepository, so transfer state
survives application restart, PC restart, and crashes. Illegal transitions
raise TransferStateError instead of silently corrupting state.
"""
from enum import Enum
from typing import List, Optional
import logging

from teloude.infrastructure.repositories import TransferRecord, TransferRepository

logger = logging.getLogger("Transfers")


class TransferState(str, Enum):
    QUEUED = "queued"
    SCANNING = "scanning"
    HASHING = "hashing"
    UPLOADING = "uploading"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    WAITING_FOR_NETWORK = "waiting_for_network"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset(
    {TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED}
)
ACTIVE = frozenset(
    {
        TransferState.QUEUED,
        TransferState.SCANNING,
        TransferState.HASHING,
        TransferState.UPLOADING,
        TransferState.DOWNLOADING,
        TransferState.PAUSED,
        TransferState.WAITING_FOR_NETWORK,
        TransferState.VERIFYING,
    }
)

# Allowed transitions (pause fast, resume via requeue, retry from terminal failure).
TRANSITIONS = {
    TransferState.QUEUED: {
        TransferState.SCANNING, TransferState.HASHING, TransferState.UPLOADING,
        TransferState.DOWNLOADING, TransferState.PAUSED, TransferState.CANCELLED,
        TransferState.FAILED,
    },
    TransferState.SCANNING: {
        TransferState.HASHING, TransferState.PAUSED, TransferState.CANCELLED,
        TransferState.FAILED, TransferState.WAITING_FOR_NETWORK,
    },
    TransferState.HASHING: {
        TransferState.UPLOADING, TransferState.PAUSED, TransferState.CANCELLED,
        TransferState.FAILED, TransferState.QUEUED,
    },
    TransferState.UPLOADING: {
        TransferState.VERIFYING, TransferState.PAUSED, TransferState.CANCELLED,
        TransferState.FAILED, TransferState.WAITING_FOR_NETWORK,
    },
    TransferState.DOWNLOADING: {
        TransferState.VERIFYING, TransferState.PAUSED, TransferState.CANCELLED,
        TransferState.FAILED, TransferState.WAITING_FOR_NETWORK,
    },
    TransferState.PAUSED: {TransferState.QUEUED, TransferState.CANCELLED},
    TransferState.WAITING_FOR_NETWORK: {
        TransferState.QUEUED, TransferState.PAUSED, TransferState.CANCELLED,
        TransferState.FAILED,
    },
    TransferState.VERIFYING: {
        TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED,
        TransferState.QUEUED,  # verification mismatch -> requeue file
    },
    TransferState.FAILED: {TransferState.QUEUED},
    TransferState.CANCELLED: {TransferState.QUEUED},
    TransferState.COMPLETED: set(),
}


class TransferStateError(Exception):
    """Raised on illegal transfer state transitions."""


class TransferRegistry:
    """State machine + persistence facade over TransferRepository."""

    def __init__(self, transfers: TransferRepository):
        self._repo = transfers

    def start_upload(
        self, file_id: int, storage_id: int, total_bytes: int, local_path: str
    ) -> TransferRecord:
        tid = self._repo.create_or_reset("upload", storage_id, file_id, total_bytes, local_path)
        rec = self._repo.get(tid)
        assert rec is not None
        return rec

    def start_download(
        self,
        storage_id: int,
        file_id: Optional[int],
        total_bytes: int,
        local_path: str,
    ) -> TransferRecord:
        tid = self._repo.create_or_reset("download", storage_id, file_id, total_bytes, local_path)
        rec = self._repo.get(tid)
        assert rec is not None
        return rec

    def transition(self, transfer_id: int, new_state: TransferState, error: Optional[str] = None) -> TransferRecord:
        rec = self._repo.get(transfer_id)
        if rec is None:
            raise TransferStateError(f"Unknown transfer id: {transfer_id}")
        current = TransferState(rec.status)
        if new_state not in TRANSITIONS[current]:
            raise TransferStateError(f"Illegal transition {current.value} -> {new_state.value}")
        self._repo.set_status(transfer_id, new_state.value, error)
        updated = self._repo.get(transfer_id)
        assert updated is not None
        logger.debug(f"Transfer {transfer_id}: {current.value} -> {new_state.value}")
        return updated

    def pause(self, transfer_id: int) -> TransferRecord:
        return self.transition(transfer_id, TransferState.PAUSED)

    def resume(self, transfer_id: int) -> TransferRecord:
        """Resume = requeue; the engine continues from the persisted checkpoint."""
        rec = self._repo.get(transfer_id)
        if rec is None:
            raise TransferStateError(f"Unknown transfer id: {transfer_id}")
        if TransferState(rec.status) is not TransferState.PAUSED:
            raise TransferStateError("Only paused transfers can resume.")
        return self.transition(transfer_id, TransferState.QUEUED)

    def cancel(self, transfer_id: int) -> TransferRecord:
        return self.transition(transfer_id, TransferState.CANCELLED)

    def fail(self, transfer_id: int, error: str) -> TransferRecord:
        return self.transition(transfer_id, TransferState.FAILED, error)

    def retry(self, transfer_id: int) -> TransferRecord:
        rec = self._repo.get(transfer_id)
        if rec is None:
            raise TransferStateError(f"Unknown transfer id: {transfer_id}")
        if TransferState(rec.status) not in (TransferState.FAILED, TransferState.CANCELLED):
            raise TransferStateError("Only failed/cancelled transfers can retry.")
        self._repo.bump_attempts(transfer_id)
        self._repo.update_progress(transfer_id, 0)
        return self.transition(transfer_id, TransferState.QUEUED)

    def checkpoint(self, transfer_id: int, done_bytes: int) -> None:
        self._repo.update_progress(transfer_id, done_bytes)

    def active(self) -> List[TransferRecord]:
        return self._repo.list_active()

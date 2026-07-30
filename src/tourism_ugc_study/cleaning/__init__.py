"""可复现的数据清洗、质量标记与重复检测。"""

from .config import CleaningConfig, ConfigurationError, load_config
from .inventory import DiscoverySummary, InventoryError, discover_increment
from .scheduler import BatchStatus, BatchSummary, SchedulerError, create_batch, get_batch_status
from .snapshot import SnapshotError, SnapshotResult, snapshot_source
from .state_machine import (
    ResumeSummary,
    StateTransitionError,
    TaskClaim,
    claim_tasks,
    finish_task,
    heartbeat_task,
    resume_batch,
)
from .text_config import TextCleaningConfig, load_text_config
from .text_normalize import NormalizedText, normalize_post_text

__all__ = [
    "CleaningConfig",
    "ConfigurationError",
    "BatchStatus",
    "BatchSummary",
    "DiscoverySummary",
    "InventoryError",
    "ResumeSummary",
    "SchedulerError",
    "SnapshotError",
    "SnapshotResult",
    "StateTransitionError",
    "TaskClaim",
    "TextCleaningConfig",
    "NormalizedText",
    "claim_tasks",
    "create_batch",
    "discover_increment",
    "finish_task",
    "get_batch_status",
    "heartbeat_task",
    "load_config",
    "load_text_config",
    "normalize_post_text",
    "resume_batch",
    "snapshot_source",
]

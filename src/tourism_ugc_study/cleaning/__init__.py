"""可复现的数据清洗、质量标记与重复检测。"""

from .config import CleaningConfig, ConfigurationError, load_config
from .inventory import DiscoverySummary, InventoryError, discover_increment
from .image_manifest import ImageManifestError
from .image_pipeline import ImageStageRunResult, sync_image_stage_tasks
from .image_repository import (
    ImageCandidateBuildResult,
    ImageFingerprintBatchResult,
    ImageManifestImportResult,
    ImageRepositoryError,
    build_image_candidates,
    import_image_manifest,
    process_image_fingerprints,
)
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
from .text_repository import (
    CandidateBuildResult,
    TextBatchResult,
    TextRepositoryError,
    TextTaskResult,
    build_text_candidates,
    process_text_tasks,
)

__all__ = [
    "CleaningConfig",
    "ConfigurationError",
    "BatchStatus",
    "BatchSummary",
    "DiscoverySummary",
    "InventoryError",
    "ImageCandidateBuildResult",
    "ImageFingerprintBatchResult",
    "ImageManifestError",
    "ImageManifestImportResult",
    "ImageRepositoryError",
    "ImageStageRunResult",
    "ResumeSummary",
    "SchedulerError",
    "SnapshotError",
    "SnapshotResult",
    "StateTransitionError",
    "TaskClaim",
    "TextCleaningConfig",
    "NormalizedText",
    "CandidateBuildResult",
    "TextBatchResult",
    "TextRepositoryError",
    "TextTaskResult",
    "claim_tasks",
    "build_text_candidates",
    "build_image_candidates",
    "create_batch",
    "discover_increment",
    "finish_task",
    "get_batch_status",
    "heartbeat_task",
    "import_image_manifest",
    "load_config",
    "load_text_config",
    "normalize_post_text",
    "process_text_tasks",
    "process_image_fingerprints",
    "resume_batch",
    "snapshot_source",
    "sync_image_stage_tasks",
]

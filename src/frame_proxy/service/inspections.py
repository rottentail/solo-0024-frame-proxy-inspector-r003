from __future__ import annotations

import hashlib

from frame_proxy.config import Settings
from frame_proxy.domain.errors import (
    ApplicationError,
    ConflictError,
    ErrorCode,
    MediaValidationError,
    StateError,
)
from frame_proxy.domain.models import (
    AttemptRecord,
    InspectionRecord,
    utc_now,
)
from frame_proxy.domain.policies import (
    InspectionRequest,
    validate_clip_id,
    validate_identifier,
)
from frame_proxy.domain.transitions import transition_inspection
from frame_proxy.media.avi import probe_avi
from frame_proxy.media.detector import AnalysisResult, analyze_frames
from frame_proxy.media.fingerprint import fingerprint_frame
from frame_proxy.media.frames import decode_frame_at
from frame_proxy.storage.repository import FileRepository


class InspectionService:
    def __init__(self, repository: FileRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def create(
        self,
        clip_id: str,
        request: InspectionRequest,
    ) -> InspectionRecord:
        normalized_clip_id = validate_clip_id(clip_id)
        clip = self._repository.get_clip(normalized_clip_id)
        if clip.state != "registered":
            raise MediaValidationError(
                "clip is not ready for inspection",
                code=ErrorCode.INVALID_STATE,
            )

        record = InspectionRecord(
            inspection_id=request.inspection_id,
            clip_id=clip.clip_id,
            state="pending",
            sensitivity=request.sensitivity,
            sample_stride=request.sample_stride,
            min_scene_frames=request.min_scene_frames,
        )
        self._repository.create_inspection(record)
        return self._run_attempt(record.inspection_id)

    def retry(self, inspection_id: str) -> InspectionRecord:
        normalized = validate_identifier(inspection_id, "inspection_id")
        return self._run_attempt(normalized)

    def get(self, inspection_id: str) -> InspectionRecord:
        normalized = validate_identifier(inspection_id, "inspection_id")
        return self._repository.get_inspection(normalized)

    def list_attempts(self, inspection_id: str) -> list[AttemptRecord]:
        return list(self.get(inspection_id).attempts)

    def begin_attempt(self, inspection_id: str) -> InspectionRecord:
        def apply(record: InspectionRecord) -> bool:
            if record.state == "indexed":
                raise StateError(
                    "an indexed inspection cannot be attempted again",
                    details={
                        "inspection_id": record.inspection_id,
                        "state": record.state,
                    },
                )
            if record.state == "scanning":
                raise ConflictError(
                    "inspection already has a running attempt",
                    details={
                        "inspection_id": record.inspection_id,
                        "active_attempt_id": record.active_attempt_id,
                    },
                )
            if record.state not in {"pending", "failed"}:
                raise StateError(
                    "inspection cannot start a new attempt",
                    details={
                        "inspection_id": record.inspection_id,
                        "state": record.state,
                    },
                )
            number = max(
                (attempt.number for attempt in record.attempts),
                default=0,
            ) + 1
            attempt = AttemptRecord(
                attempt_id=f"attempt-{number:04d}",
                number=number,
                state="running",
            )
            record.attempts.append(attempt)
            record.active_attempt_id = attempt.attempt_id
            transition_inspection(
                record,
                "scanning",
                reason=f"attempt {number} started",
            )
            return True

        return self._repository.mutate_inspection(inspection_id, apply)

    def complete_attempt(
        self,
        inspection_id: str,
        attempt_id: str,
        result: AnalysisResult,
    ) -> InspectionRecord:
        def apply(record: InspectionRecord) -> bool:
            if (
                record.state != "scanning"
                or record.active_attempt_id != attempt_id
            ):
                raise ConflictError(
                    "inspection attempt is no longer active",
                    details={
                        "inspection_id": record.inspection_id,
                        "attempt_id": attempt_id,
                        "state": record.state,
                    },
                )
            record.sampled_frames = list(result.sampled_frames)
            record.cut_frames = list(result.cut_frames)
            record.scenes = list(result.scenes)
            self._close_attempt(record, attempt_id, "succeeded")
            record.active_attempt_id = None
            record.error_code = None
            record.error_message = None
            transition_inspection(
                record,
                "indexed",
                reason="scene ranges verified",
            )
            return True

        return self._repository.mutate_inspection(inspection_id, apply)

    def fail_attempt(
        self,
        inspection_id: str,
        attempt_id: str,
        code: str,
        message: str,
    ) -> bool:
        applied = False

        def apply(record: InspectionRecord) -> bool:
            nonlocal applied
            if (
                record.state != "scanning"
                or record.active_attempt_id != attempt_id
            ):
                return False
            self._close_attempt(record, attempt_id, "failed", code, message)
            record.active_attempt_id = None
            record.error_code = code
            record.error_message = message
            transition_inspection(record, "failed", reason=code)
            applied = True
            return True

        try:
            self._repository.mutate_inspection(inspection_id, apply)
        except Exception:
            return False
        return applied

    def recover_interrupted(self) -> list[str]:
        recovered: list[str] = []
        for summary in self._repository.list_inspections():
            if summary.state not in {"pending", "scanning"}:
                continue
            changed = False

            def apply(record: InspectionRecord) -> bool:
                nonlocal changed
                if record.state == "scanning":
                    message = "service restarted while the attempt was running"
                    active_id = record.active_attempt_id
                    if active_id is not None:
                        self._close_attempt(
                            record,
                            active_id,
                            "interrupted",
                            ErrorCode.INSPECTION_INTERRUPTED.value,
                            message,
                        )
                    record.active_attempt_id = None
                    record.error_code = ErrorCode.INSPECTION_INTERRUPTED.value
                    record.error_message = message
                    transition_inspection(
                        record,
                        "failed",
                        reason=ErrorCode.INSPECTION_INTERRUPTED.value,
                    )
                    changed = True
                elif record.state == "pending":
                    record.error_code = ErrorCode.INSPECTION_INTERRUPTED.value
                    record.error_message = (
                        "service restarted before the first attempt started"
                    )
                    transition_inspection(
                        record,
                        "failed",
                        reason=ErrorCode.INSPECTION_INTERRUPTED.value,
                    )
                    changed = True
                return changed

            self._repository.mutate_inspection(summary.inspection_id, apply)
            if changed:
                recovered.append(summary.inspection_id)
        return recovered

    def _run_attempt(self, inspection_id: str) -> InspectionRecord:
        record = self.begin_attempt(inspection_id)
        attempt_id = str(record.active_attempt_id)
        try:
            clip = self._repository.get_clip(record.clip_id)
            if clip.state != "registered":
                raise MediaValidationError(
                    "clip is not ready for inspection",
                    code=ErrorCode.INVALID_STATE,
                )
            media = self._repository.get_clip_media(clip.clip_id)
            if hashlib.sha256(media).hexdigest() != clip.digest:
                raise MediaValidationError(
                    "stored clip digest does not match its record",
                    code=ErrorCode.FRAME_DECODE,
                )
            descriptor = probe_avi(media)
            sampled_frames = tuple(
                range(0, descriptor.frame_count, record.sample_stride)
            )
            fingerprints = tuple(
                fingerprint_frame(decode_frame_at(media, descriptor, frame_index))
                for frame_index in sampled_frames
            )
            result = analyze_frames(
                descriptor.frame_count,
                sampled_frames,
                fingerprints,
                sensitivity=record.sensitivity,
                min_scene_frames=record.min_scene_frames,
            )
            return self.complete_attempt(inspection_id, attempt_id, result)
        except ApplicationError as exc:
            self.fail_attempt(inspection_id, attempt_id, exc.code.value, exc.message)
            raise
        except Exception as exc:
            self.fail_attempt(
                inspection_id,
                attempt_id,
                ErrorCode.INTERNAL.value,
                str(exc),
            )
            raise ApplicationError(
                ErrorCode.INTERNAL,
                "shot indexing failed",
                status=500,
            ) from exc

    @staticmethod
    def _close_attempt(
        record: InspectionRecord,
        attempt_id: str,
        state: str,
        code: str | None = None,
        message: str | None = None,
    ) -> bool:
        for attempt in reversed(record.attempts):
            if attempt.attempt_id == attempt_id:
                attempt.state = state
                attempt.finished_at = utc_now()
                attempt.error_code = code
                attempt.error_message = message
                return True
        return False

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from frame_proxy.config import Settings
from frame_proxy.domain.errors import ConflictError
from frame_proxy.domain.models import InspectionRecord
from frame_proxy.media.avi import probe_avi
from frame_proxy.media.detector import analyze_frames
from frame_proxy.media.fingerprint import fingerprint_frame
from frame_proxy.media.frames import decode_frame_at
from frame_proxy.service.inspections import InspectionService
from frame_proxy.storage.repository import FileRepository
from tools.harness import RunningService, require, require_status, success

INSPECTION_BODY = {
    "sensitivity": 0.55,
    "sample_stride": 1,
    "min_scene_frames": 2,
}

SHEET_BODY = {
    "sheet_id": "sheet-retry-a",
    "columns": 3,
    "tile_width": 96,
    "gutter": 4,
    "selection": "stored",
    "background": "#20252c",
}


def _break_stored_media(data_dir: Path, clip_id: str) -> bytes:
    media_path = data_dir / "clips" / clip_id / "clip.avi"
    original = media_path.read_bytes()
    corrupted = bytearray(original)
    corrupted[len(corrupted) // 2] ^= 0xFF
    media_path.write_bytes(bytes(corrupted))
    return original


def _restore_stored_media(data_dir: Path, clip_id: str, payload: bytes) -> None:
    (data_dir / "clips" / clip_id / "clip.avi").write_bytes(payload)


def _attempt_states(inspection: dict) -> list[str]:
    return [attempt["state"] for attempt in inspection["attempts"]]


def check_retry_after_failure() -> None:
    with RunningService() as service:
        require_status(
            service.upload_clip("clip-retry-a"),
            201,
            "clip setup",
        )
        original = _break_stored_media(service.data_dir, "clip-retry-a")
        failed = service.request_json(
            "POST",
            "/v1/clips/clip-retry-a/inspections",
            {"inspection_id": "inspection-retry-a", **INSPECTION_BODY},
        )
        require_status(failed, 422, "broken media inspection")
        require(
            failed.json()["error"]["code"] == "frame_decode_failed",
            "broken media returned the wrong error code",
        )

        inspection = service.request(
            "GET",
            "/v1/inspections/inspection-retry-a",
        ).json()["inspection"]
        require(inspection["state"] == "failed", "inspection did not fail")
        require(
            inspection["error_code"] == "frame_decode_failed",
            "failed inspection lost its stable error code",
        )
        require(
            _attempt_states(inspection) == ["failed"],
            "first attempt was not recorded as failed",
        )
        require(
            inspection["attempts"][0]["error_code"] == "frame_decode_failed",
            "first attempt lost its failure reason",
        )
        require(
            "active_attempt_id" not in inspection,
            "failed inspection still exposes an active attempt",
        )

        early_sheet = service.request_json(
            "POST",
            "/v1/inspections/inspection-retry-a/contact-sheets",
            SHEET_BODY,
        )
        require_status(early_sheet, 409, "contact sheet on failed inspection")

        _restore_stored_media(service.data_dir, "clip-retry-a", original)
        retried = service.request_json(
            "POST",
            "/v1/inspections/inspection-retry-a/attempts",
            {},
        )
        require_status(retried, 201, "inspection retry")
        inspection = retried.json()["inspection"]
        require(
            inspection["state"] == "indexed",
            "retried inspection did not become indexed",
        )
        require(
            inspection["cut_frames"] == [5, 9, 14],
            f"unexpected cuts after retry: {inspection['cut_frames']}",
        )
        require(
            _attempt_states(inspection) == ["failed", "succeeded"],
            "retry did not append a succeeded attempt",
        )
        require(
            inspection["attempts"][0]["error_code"] == "frame_decode_failed",
            "retry erased the historical failure reason",
        )
        require(
            inspection["attempts"][0]["finished_at"]
            <= inspection["attempts"][1]["started_at"],
            "attempt timestamps are not ordered",
        )
        require(
            "error_code" not in inspection,
            "indexed inspection still exposes a stale error code",
        )

        listed = service.request(
            "GET",
            "/v1/inspections/inspection-retry-a/attempts",
        )
        require_status(listed, 200, "attempt history lookup")
        require(
            _attempt_states(listed.json()) == ["failed", "succeeded"],
            "attempt history endpoint disagrees with the record",
        )

        again = service.request_json(
            "POST",
            "/v1/inspections/inspection-retry-a/attempts",
            {},
        )
        require_status(again, 409, "retry of an indexed inspection")

        sheet = service.request_json(
            "POST",
            "/v1/inspections/inspection-retry-a/contact-sheets",
            SHEET_BODY,
        )
        require_status(sheet, 201, "contact sheet after retry")
        record = sheet.json()["contact_sheet"]
        require(
            len(record["tile_map"]) == len(inspection["scenes"]),
            "contact sheet did not consume the committed scene result",
        )


def check_restart_recovery() -> None:
    with tempfile.TemporaryDirectory(prefix="frame-proxy-retry-") as directory:
        data_dir = Path(directory)
        with RunningService(data_dir) as service:
            require_status(
                service.upload_clip("clip-restart-a"),
                201,
                "clip setup",
            )

        # Simulate a crash mid-scan: a persisted scanning record whose
        # attempt is still marked running, produced by production code.
        repository = FileRepository(data_dir)
        direct = InspectionService(repository, Settings(data_root=data_dir))
        repository.create_inspection(
            InspectionRecord(
                inspection_id="inspection-restart-a",
                clip_id="clip-restart-a",
                state="pending",
                sensitivity=INSPECTION_BODY["sensitivity"],
                sample_stride=INSPECTION_BODY["sample_stride"],
                min_scene_frames=INSPECTION_BODY["min_scene_frames"],
            )
        )
        began = direct.begin_attempt("inspection-restart-a")
        require(began.state == "scanning", "attempt did not start")
        require(
            began.active_attempt_id == "attempt-0001",
            "unexpected first attempt identifier",
        )

        with RunningService(data_dir) as service:
            inspection = service.request(
                "GET",
                "/v1/inspections/inspection-restart-a",
            ).json()["inspection"]
            require(
                inspection["state"] == "failed",
                "restart did not close the interrupted inspection",
            )
            require(
                inspection["error_code"] == "inspection_interrupted",
                "restart recovery used the wrong error code",
            )
            require(
                _attempt_states(inspection) == ["interrupted"],
                "interrupted attempt was not recorded",
            )
            require(
                inspection["attempts"][0]["error_code"] == "inspection_interrupted",
                "interrupted attempt lost its reason",
            )

            retried = service.request_json(
                "POST",
                "/v1/inspections/inspection-restart-a/attempts",
                {},
            )
            require_status(retried, 201, "retry after restart")
            inspection = retried.json()["inspection"]
            require(
                inspection["state"] == "indexed",
                "inspection did not finish after restart recovery",
            )
            require(
                _attempt_states(inspection) == ["interrupted", "succeeded"],
                "restart attempt history is incomplete",
            )
            require(
                inspection["cut_frames"] == [5, 9, 14],
                "recovered inspection produced unexpected cuts",
            )


def check_concurrent_retries() -> None:
    with RunningService() as service:
        require_status(
            service.upload_clip("clip-retry-b"),
            201,
            "clip setup",
        )
        original = _break_stored_media(service.data_dir, "clip-retry-b")
        failed = service.request_json(
            "POST",
            "/v1/clips/clip-retry-b/inspections",
            {"inspection_id": "inspection-retry-b", **INSPECTION_BODY},
        )
        require_status(failed, 422, "broken media inspection")
        _restore_stored_media(service.data_dir, "clip-retry-b", original)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: service.request_json(
                        "POST",
                        "/v1/inspections/inspection-retry-b/attempts",
                        {},
                    ),
                    range(2),
                )
            )
        statuses = sorted(result.status for result in results)
        require(
            statuses == [201, 409],
            f"concurrent retries returned {statuses}",
        )

        inspection = service.request(
            "GET",
            "/v1/inspections/inspection-retry-b",
        ).json()["inspection"]
        require(
            inspection["state"] == "indexed",
            "concurrent retries did not produce one indexed result",
        )
        require(
            _attempt_states(inspection) == ["failed", "succeeded"],
            "concurrent retries committed more than one attempt",
        )


def check_stale_attempt_fencing() -> None:
    with RunningService() as service:
        require_status(
            service.upload_clip("clip-retry-c"),
            201,
            "clip setup",
        )
        original = _break_stored_media(service.data_dir, "clip-retry-c")
        failed = service.request_json(
            "POST",
            "/v1/clips/clip-retry-c/inspections",
            {"inspection_id": "inspection-retry-c", **INSPECTION_BODY},
        )
        require_status(failed, 422, "broken media inspection")
        _restore_stored_media(service.data_dir, "clip-retry-c", original)

        direct = InspectionService(
            FileRepository(service.data_dir),
            Settings(data_root=service.data_dir),
        )
        stale = direct.begin_attempt("inspection-retry-c")
        stale_id = str(stale.active_attempt_id)
        require(
            direct.recover_interrupted() == ["inspection-retry-c"],
            "recovery did not interrupt the in-flight attempt",
        )
        current = direct.begin_attempt("inspection-retry-c")
        current_id = str(current.active_attempt_id)

        media = (service.data_dir / "clips" / "clip-retry-c" / "clip.avi").read_bytes()
        descriptor = probe_avi(media)
        sampled = tuple(range(0, descriptor.frame_count, 1))
        fingerprints = tuple(
            fingerprint_frame(decode_frame_at(media, descriptor, frame_index))
            for frame_index in sampled
        )
        result = analyze_frames(
            descriptor.frame_count,
            sampled,
            fingerprints,
            sensitivity=INSPECTION_BODY["sensitivity"],
            min_scene_frames=INSPECTION_BODY["min_scene_frames"],
        )

        try:
            direct.complete_attempt("inspection-retry-c", stale_id, result)
            raise RuntimeError("stale attempt was allowed to commit")
        except ConflictError:
            pass
        require(
            direct.fail_attempt(
                "inspection-retry-c",
                stale_id,
                "internal_failure",
                "late failure",
            )
            is False,
            "stale attempt was allowed to fail the record",
        )
        record = direct.get("inspection-retry-c")
        require(
            record.state == "scanning" and record.active_attempt_id == current_id,
            "stale attempt disturbed the active attempt",
        )

        direct.complete_attempt("inspection-retry-c", current_id, result)
        record = direct.get("inspection-retry-c")
        require(record.state == "indexed", "active attempt did not commit")
        require(
            [attempt.state for attempt in record.attempts]
            == ["failed", "interrupted", "succeeded"],
            "attempt history is not append-only",
        )
        committed_scenes = [scene.as_dict() for scene in record.scenes]

        require(
            direct.fail_attempt(
                "inspection-retry-c",
                stale_id,
                "internal_failure",
                "late failure",
            )
            is False,
            "late failure reached an indexed inspection",
        )
        record = direct.get("inspection-retry-c")
        require(
            record.state == "indexed"
            and [scene.as_dict() for scene in record.scenes] == committed_scenes,
            "committed scene result changed after a late attempt",
        )


def main() -> int:
    check_retry_after_failure()
    check_restart_recovery()
    check_concurrent_retries()
    check_stale_attempt_fencing()
    success(
        "retry-failed-inspection",
        {
            "attempts": "append-only",
            "recovery": "interrupted attempts closed on startup",
            "result": "single committed scene result",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

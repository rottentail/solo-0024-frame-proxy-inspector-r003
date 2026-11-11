from __future__ import annotations

from frame_proxy.config import Settings
from frame_proxy.http.router import RequestRouter
from frame_proxy.service.clips import ClipService
from frame_proxy.service.contact_sheets import ContactSheetService
from frame_proxy.service.inspections import InspectionService
from frame_proxy.storage.repository import FileRepository


def build_router(settings: Settings) -> RequestRouter:
    settings.ensure_ready()
    repository = FileRepository(settings.data_root)
    inspections = InspectionService(repository, settings)
    inspections.recover_interrupted()
    return RequestRouter(
        settings=settings,
        clips=ClipService(repository, settings),
        inspections=inspections,
        contact_sheets=ContactSheetService(repository, settings),
    )


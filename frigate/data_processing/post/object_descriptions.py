"""Post processor for object descriptions using GenAI."""

import datetime
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np
from peewee import DoesNotExist

from frigate.comms.inter_process import InterProcessRequestor
from frigate.config import CameraConfig, FrigateConfig
from frigate.const import CLIPS_DIR, UPDATE_EVENT_DESCRIPTION
from frigate.data_processing.post.description_recovery import (
    ConditionalWriteResult,
    RecoveryAdmission,
    dispatcher_write_succeeded,
    persist_object_description,
)
from frigate.data_processing.post.description_work_queue import (
    DescriptionWorkQueue,
)
from frigate.data_processing.post.semantic_trigger import SemanticTriggerProcessor
from frigate.data_processing.types import PostProcessDataEnum
from frigate.genai.manager import GenAIClientManager
from frigate.genai.request_outcome import (
    AttemptOutcome,
    consume_outcome,
    reset_outcome,
)
from frigate.models import Event
from frigate.types import TrackedObjectUpdateTypesEnum
from frigate.util.builtin import EventsPerSecond, InferenceSpeed
from frigate.util.file import get_event_thumbnail_bytes, load_event_snapshot_image
from frigate.util.image import create_thumbnail, ensure_jpeg_bytes

if TYPE_CHECKING:
    from frigate.embeddings.embeddings import Embeddings

from ..post.api import PostProcessorApi
from ..types import DataProcessorMetrics

logger = logging.getLogger(__name__)

MAX_THUMBNAILS = 10


class ObjectDescriptionProcessor(PostProcessorApi):
    def __init__(
        self,
        config: FrigateConfig,
        embeddings: "Embeddings",
        requestor: InterProcessRequestor,
        metrics: DataProcessorMetrics,
        genai_manager: GenAIClientManager,
        semantic_trigger_processor: SemanticTriggerProcessor | None,
        description_queue: DescriptionWorkQueue,
    ):
        super().__init__(config, metrics, None)
        self.config = config
        self.embeddings = embeddings
        self.requestor = requestor
        self.metrics = metrics
        self.genai_manager = genai_manager
        self.semantic_trigger_processor = semantic_trigger_processor
        self.description_queue = description_queue
        self.description_recovery = None
        self.tracked_events: dict[str, list[Any]] = {}
        self.early_request_sent: dict[str, bool] = {}
        self.object_desc_speed = InferenceSpeed(self.metrics.object_desc_speed)
        self.object_desc_dps = EventsPerSecond()
        self.object_desc_dps.start()

    def __handle_frame_update(
        self, camera: str, data: dict, yuv_frame: np.ndarray
    ) -> None:
        """Handle an update to a frame for an object."""
        camera_config = self.config.cameras[camera]

        if not camera_config.objects.genai.enabled:
            return

        # no need to save our own thumbnails if the object has become stationary
        if not data["stationary"]:
            if data["id"] not in self.tracked_events:
                self.tracked_events[data["id"]] = []

            data["thumbnail"] = create_thumbnail(yuv_frame, data["box"])

            # Limit the number of thumbnails saved
            if len(self.tracked_events[data["id"]]) >= MAX_THUMBNAILS:
                # Always keep the first thumbnail for the event
                self.tracked_events[data["id"]].pop(1)

            self.tracked_events[data["id"]].append(data)

        # check if we're configured to send an early request after a minimum number of updates received
        if camera_config.objects.genai.send_triggers.after_significant_updates:
            if (
                len(self.tracked_events.get(data["id"], []))
                >= camera_config.objects.genai.send_triggers.after_significant_updates
                and data["id"] not in self.early_request_sent
            ):
                if data["has_clip"] and data["has_snapshot"]:
                    try:
                        event: Event = Event.get(Event.id == data["id"])
                    except DoesNotExist:
                        logger.error(f"Event {data['id']} not found")
                        return

                    if (
                        not camera_config.objects.genai.objects
                        or event.label in camera_config.objects.genai.objects
                    ) and (
                        not camera_config.objects.genai.required_zones
                        or set(data["entered_zones"])
                        & set(camera_config.objects.genai.required_zones)
                    ):
                        logger.debug(f"{camera} sending early request to GenAI")

                        self.early_request_sent[data["id"]] = True
                        # Copy thumbnails to avoid holding references after cleanup
                        thumbnails_copy = [
                            data["thumbnail"][:] if data.get("thumbnail") else None
                            for data in self.tracked_events[data["id"]]
                            if data.get("thumbnail")
                        ]
                        self._submit_description(event, thumbnails_copy)

    def __handle_frame_finalize(
        self, camera: str, event: Event, thumbnail: bytes
    ) -> None:
        """Handle the finalization of a frame."""
        camera_config = self.config.cameras[camera]

        if (
            camera_config.objects.genai.enabled
            and camera_config.objects.genai.send_triggers.tracked_object_end
            and (
                not camera_config.objects.genai.objects
                or event.label in camera_config.objects.genai.objects
            )
            and (
                not camera_config.objects.genai.required_zones
                or set(event.zones) & set(camera_config.objects.genai.required_zones)
            )
        ):
            self._process_genai_description(event, camera_config, thumbnail)
        else:
            self.cleanup_event(str(event.id))

    def __regenerate_description(self, event_id: str, source: str, force: bool) -> None:
        """Regenerate the description for an event."""
        try:
            event: Event = Event.get(Event.id == event_id)
        except DoesNotExist:
            logger.error(f"Event {event_id} not found for description regeneration")
            return

        camera_config = self.config.cameras[str(event.camera)]
        if not camera_config.objects.genai.enabled and not force:
            logger.error(f"GenAI not enabled for camera {event.camera}")
            return

        thumbnail = get_event_thumbnail_bytes(event)

        if thumbnail is None:
            logger.error("No thumbnail available for %s", event.id)
            return

        # ensure we have a jpeg to pass to the model
        thumbnail = ensure_jpeg_bytes(thumbnail)

        logger.debug(
            f"Trying {source} regeneration for {event}, has_snapshot: {event.has_snapshot}"
        )

        if event.has_snapshot and source == "snapshot":
            snapshot_image = self._read_and_crop_snapshot(event)
            if not snapshot_image:
                return

        embed_image = (
            [snapshot_image]
            if event.has_snapshot and source == "snapshot"
            # Copy thumbnails to avoid holding references
            else (
                [
                    data["thumbnail"][:] if data.get("thumbnail") else None
                    for data in self.tracked_events[event_id]
                    if data.get("thumbnail")
                ]
                if len(self.tracked_events.get(event_id, [])) > 0
                else [thumbnail]
            )
        )

        self._submit_description(event, [img for img in embed_image if img is not None])

    def process_data(self, frame_data: dict, data_type: PostProcessDataEnum) -> None:
        """Process a frame update."""
        self.metrics.object_desc_dps.value = self.object_desc_dps.eps()

        if data_type != PostProcessDataEnum.tracked_object:
            return

        state: str | None = frame_data.get("state", None)

        if state is not None:
            logger.debug(f"Processing {state} for {frame_data['camera']}")

        if state == "update":
            self.__handle_frame_update(
                frame_data["camera"], frame_data["data"], frame_data["yuv_frame"]
            )
        elif state == "finalize":
            self.__handle_frame_finalize(
                frame_data["camera"], frame_data["event"], frame_data["thumbnail"]
            )

    def handle_request(self, topic: str, data: dict[str, Any]) -> str | None:
        """Handle a request."""
        if topic == "regenerate_description":
            self.__regenerate_description(
                data["event_id"], data["source"], data["force"]
            )
        return None

    def cleanup_event(self, event_id: str) -> None:
        """Clean up tracked event data to prevent memory leaks.

        This should be called when an event ends, regardless of whether
        genai processing is triggered.
        """
        if event_id in self.tracked_events:
            del self.tracked_events[event_id]
        if event_id in self.early_request_sent:
            del self.early_request_sent[event_id]

    def _read_and_crop_snapshot(self, event: Event) -> bytes | None:
        """Read, decode, and crop the snapshot image."""

        try:
            img, _ = load_event_snapshot_image(event)
            if img is None:
                logger.error(f"Cannot load snapshot for {event.id}, file not found")
                return None

            # Crop snapshot based on region
            # provide full image if region doesn't exist (manual events)
            height, width = img.shape[:2]
            x1_rel, y1_rel, width_rel, height_rel = event.data.get(  # type: ignore[attr-defined]
                "region", [0, 0, 1, 1]
            )
            x1, y1 = int(x1_rel * width), int(y1_rel * height)

            cropped_image = img[
                y1 : y1 + int(height_rel * height),
                x1 : x1 + int(width_rel * width),
            ]

            _, buffer = cv2.imencode(".jpg", cropped_image)

            return buffer.tobytes()
        except Exception:
            return None

    def _process_genai_description(
        self, event: Event, camera_config: CameraConfig, thumbnail: bytes
    ) -> None:
        event_id = str(event.id)

        if event.has_snapshot and camera_config.objects.genai.use_snapshot:
            snapshot_image = self._read_and_crop_snapshot(event)

            if not snapshot_image:
                self.cleanup_event(event_id)
                return

        num_thumbnails = len(self.tracked_events.get(event_id, []))

        # ensure we have a jpeg to pass to the model
        thumbnail = ensure_jpeg_bytes(thumbnail)

        embed_image = (
            [snapshot_image]
            if event.has_snapshot and camera_config.objects.genai.use_snapshot
            # Copy thumbnails to avoid holding references after cleanup
            else (
                [
                    data["thumbnail"][:] if data.get("thumbnail") else None
                    for data in self.tracked_events[event_id]
                    if data.get("thumbnail")
                ]
                if num_thumbnails > 0
                else [thumbnail]
            )
        )

        if camera_config.objects.genai.debug_save_thumbnails and num_thumbnails > 0:
            logger.debug(f"Saving {num_thumbnails} thumbnails for event {event_id}")

            Path(os.path.join(CLIPS_DIR, f"genai-requests/{event_id}")).mkdir(
                parents=True, exist_ok=True
            )

            for idx, data in enumerate(self.tracked_events[event_id], 1):
                jpg_bytes: bytes | None = data["thumbnail"]

                if jpg_bytes is None:
                    logger.warning(f"Unable to save thumbnail {idx} for {event_id}.")
                else:
                    with open(
                        os.path.join(
                            CLIPS_DIR,
                            f"genai-requests/{event_id}/{idx}.jpg",
                        ),
                        "wb",
                    ) as j:
                        j.write(jpg_bytes)

        # The shared bounded worker owns all description provider calls.
        self._submit_description(event, [img for img in embed_image if img is not None])

        # Clean up tracked events and early request state
        self.cleanup_event(event_id)

    def _record_live_failure(
        self,
        event_id: Any,
        outcome: AttemptOutcome,
    ) -> None:
        if outcome != AttemptOutcome.success and self.description_recovery is not None:
            self.description_recovery.note_live_failure("object", event_id)

    def _submit_description(
        self,
        event: Event,
        thumbnails: list[bytes],
        recovery: bool = False,
        on_complete: Any | None = None,
    ) -> bool:
        """Freeze media and submit one idempotent job to the shared queue."""
        frozen_thumbnails = tuple(bytes(image) for image in thumbnails if image)
        state: dict[str, str] = {}
        completion_callback = on_complete
        if completion_callback is None and not recovery:

            def record_failure(outcome: AttemptOutcome) -> None:
                self._record_live_failure(event.id, outcome)

            completion_callback = record_failure
        submitter = (
            self.description_queue.submit_recovery
            if recovery
            else self.description_queue.submit
        )
        accepted = submitter(
            "object",
            lambda worker_requestor: self._genai_embed_description(
                event,
                frozen_thumbnails,
                worker_requestor,
                recovery,
                state,
            ),
            on_complete=completion_callback,
        )
        if not accepted and not recovery and self.description_recovery is not None:
            self.description_recovery.note_live_failure("object", event.id)
        return accepted

    def submit_recovery(
        self,
        event: Event,
        on_complete: Any,
    ) -> RecoveryAdmission:
        """Submit one eligible missing object description without notifications."""
        try:
            event = Event.get_by_id(event.id)
            camera_config = self.config.cameras.get(str(event.camera))
            data = event.data if isinstance(event.data, dict) else {}
            if (
                camera_config is None
                or not camera_config.objects.genai.enabled
                or not camera_config.objects.genai.send_triggers.tracked_object_end
                or (data.get("description") or "").strip()
                or (
                    camera_config.objects.genai.objects
                    and event.label not in camera_config.objects.genai.objects
                )
                or (
                    camera_config.objects.genai.required_zones
                    and not set(event.zones)
                    & set(camera_config.objects.genai.required_zones)
                )
            ):
                return RecoveryAdmission.terminal_skip

            thumbnail = get_event_thumbnail_bytes(event)
            if thumbnail is None:
                return RecoveryAdmission.retry_later
            thumbnail = ensure_jpeg_bytes(thumbnail)
            images = [thumbnail]
            if event.has_snapshot and camera_config.objects.genai.use_snapshot:
                snapshot = self._read_and_crop_snapshot(event)
                if snapshot is None:
                    return RecoveryAdmission.retry_later
                images = [snapshot]
            return (
                RecoveryAdmission.accepted
                if self._submit_description(
                    event,
                    images,
                    recovery=True,
                    on_complete=on_complete,
                )
                else RecoveryAdmission.retry_later
            )
        except Exception as error:
            logger.warning(
                "Object description recovery candidate skipped (%s)",
                type(error).__name__,
            )
            return RecoveryAdmission.retry_later

    def _genai_embed_description(
        self,
        event: Event,
        thumbnails: tuple[bytes, ...],
        requestor: InterProcessRequestor,
        recovery: bool,
        state: dict[str, str],
    ) -> AttemptOutcome:
        """Run one retryable object generation or persistence attempt."""
        if not thumbnails:
            return AttemptOutcome.invalid_input

        start = datetime.datetime.now().timestamp()
        try:
            if recovery:
                event = Event.get_by_id(event.id)
                data = event.data if isinstance(event.data, dict) else {}
                if (data.get("description") or "").strip():
                    return AttemptOutcome.success

            description = state.get("description")
            if description is None:
                client = self.genai_manager.description_client
                if client is None:
                    return AttemptOutcome.provider_unavailable
                camera_config = self.config.cameras[str(event.camera)]
                reset_outcome()
                try:
                    generated = client.generate_object_description(
                        camera_config, list(thumbnails), event
                    )
                except Exception:
                    return AttemptOutcome.internal_error
                provider_outcome = consume_outcome()
                if not generated or not generated.strip():
                    return (
                        AttemptOutcome.empty
                        if provider_outcome == AttemptOutcome.success
                        else provider_outcome
                    )
                description = generated.strip()
                state["description"] = description

            try:
                if recovery:
                    write_result = persist_object_description(
                        str(event.id), description
                    )
                    if write_result == ConditionalWriteResult.already_present:
                        return AttemptOutcome.success
                    persisted = write_result == ConditionalWriteResult.written
                else:
                    response = requestor.send_data(
                        UPDATE_EVENT_DESCRIPTION,
                        {
                            "type": TrackedObjectUpdateTypesEnum.description,
                            "id": event.id,
                            "description": description,
                            "camera": event.camera,
                        },
                    )
                    persisted = dispatcher_write_succeeded(response)
            except Exception:
                persisted = False
            if not persisted:
                return AttemptOutcome.persistence_error

            if self.config.semantic_search.enabled and self.embeddings is not None:
                try:
                    self.embeddings.embed_description(str(event.id), description)
                    if not recovery and self.semantic_trigger_processor is not None:
                        self.semantic_trigger_processor.process_data(
                            {
                                "event_id": event.id,
                                "camera": event.camera,
                                "type": "text",
                            },
                            PostProcessDataEnum.tracked_object,
                        )
                except Exception as error:
                    logger.warning(
                        "Object description embedding failed (%s)",
                        type(error).__name__,
                    )

            self.object_desc_speed.update(datetime.datetime.now().timestamp() - start)
            self.object_desc_dps.update()
            logger.debug(
                "Generated object description (%d images, recovery=%s)",
                len(thumbnails),
                recovery,
            )
            return AttemptOutcome.success
        except Exception as error:
            logger.warning(
                "Object description attempt failed (%s)", type(error).__name__
            )
            return AttemptOutcome.internal_error

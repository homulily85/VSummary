"""State transitions shared by durable summary workers."""

from datetime import UTC, datetime, timedelta

from vsummary.model.channel import JobStatus, SummaryJob
from vsummary.settings import RuntimeConfig


def source_failure(
    job: SummaryJob,
    exc: Exception,
    config: RuntimeConfig,
    *,
    permanent: bool = False,
    allow_youtube_fallback: bool = False,
) -> bool:
    """Apply source retry policy; return whether a failure notice is required."""
    job.retry_count += 1
    job.last_error = str(exc)
    if (
        allow_youtube_fallback
        and not permanent
        and not job.youtube_audio_fallback
        and job.source == "youtube"
        and job.retry_count >= config.source_retry_limit
        and config.youtube_yt_dlp_fallback_enabled
    ):
        job.youtube_audio_fallback = True
        job.status = JobStatus.QUEUED
        job.next_attempt_at = datetime.now(UTC)
        return False
    if (
        permanent
        or job.youtube_audio_fallback
        or job.retry_count >= config.source_retry_limit
    ):
        return True
    job.status = JobStatus.QUEUED
    job.next_attempt_at = datetime.now(UTC) + timedelta(
        seconds=config.retry_delay_seconds(job.retry_count)
    )
    return False


def delivery_failure(job: SummaryJob, exc: Exception, config: RuntimeConfig) -> None:
    """Preserve payload and progress while scheduling another delivery attempt."""
    job.delivery_retry_count += 1
    job.last_error = str(exc)
    job.status = JobStatus.FAILED
    if job.delivery_retry_count < config.delivery_retry_limit:
        job.status = JobStatus.READY_TO_DELIVER
        job.next_attempt_at = datetime.now(UTC) + timedelta(
            seconds=config.retry_delay_seconds(job.delivery_retry_count)
        )

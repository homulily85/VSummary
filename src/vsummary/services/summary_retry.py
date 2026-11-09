"""Job state changes after generation and Discord delivery failures."""

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
    """Record a generation failure and choose the next action.

    Mutate the job in memory. A retryable failure can schedule a backoff or an immediate
    YouTube audio fallback when native retries are exhausted.

    Args:
        job: Job whose generation attempt failed.
        exc: Error recorded as the job's last failure.
        config: Retry limits, delay parameters, and YouTube fallback setting.
        permanent: Whether the failure should immediately require a notice.
        allow_youtube_fallback: Whether this request is eligible for audio fallback.

    Returns:
        ``True`` when the caller should prepare a failure notice, or ``False`` when
            another generation attempt is scheduled.
    """
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
    """Record a delivery failure and reschedule it if attempts remain.

    Update the job's retry count, error, and state in memory. Retain its saved payload
    and chunk position so a retry can resume.

    Args:
        job: Job with saved Discord delivery state.
        exc: Error from the delivery attempt.
        config: Delivery retry limit and backoff settings.
    """
    job.delivery_retry_count += 1
    job.last_error = str(exc)
    job.status = JobStatus.FAILED
    if job.delivery_retry_count < config.delivery_retry_limit:
        job.status = JobStatus.READY_TO_DELIVER
        job.next_attempt_at = datetime.now(UTC) + timedelta(
            seconds=config.retry_delay_seconds(job.delivery_retry_count)
        )

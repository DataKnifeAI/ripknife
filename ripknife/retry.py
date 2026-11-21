"""Retry helpers and settings for transient disc/NAS failures.

``count`` / ``nas_retries`` mean *extra* attempts after the first failure.
Default ``count=1`` → 1 retry = **2 total attempts** (initial + 1 retry)
before giving up. TV slot failures continue to the next episode; movies still
fail the whole rip.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from ripknife.monitor import (
    ALERT_PREFIX,
    DiscOpenError,
    MakeMkvProcessError,
    RipStallError,
)

logger = logging.getLogger("ripknife")

T = TypeVar("T")

DEFAULT_RETRY_COUNT = 1
DEFAULT_RETRY_DELAY_SEC = 30.0
DEFAULT_NAS_RETRIES = 1
DEFAULT_STALL_RETRY = True


@dataclass(frozen=True)
class RetrySettings:
    """How many extra attempts and how long to wait between them.

    ``count`` is retries after the first failure (1 → 2 total attempts).
    """

    count: int = DEFAULT_RETRY_COUNT
    delay_sec: float = DEFAULT_RETRY_DELAY_SEC
    stall_retry: bool = DEFAULT_STALL_RETRY
    nas_retries: int = DEFAULT_NAS_RETRIES

    @classmethod
    def resolve(
        cls,
        manifest_retry: RetrySettings | None = None,
        *,
        retry_cli: int | None = None,
        delay_cli: float | None = None,
        stall_retry_cli: bool | None = None,
    ) -> RetrySettings:
        base = manifest_retry or cls()
        count = (
            retry_cli
            if retry_cli is not None
            else int(os.environ.get("RIPKNIFE_RETRY_COUNT", base.count))
        )
        delay = (
            delay_cli
            if delay_cli is not None
            else float(os.environ.get("RIPKNIFE_RETRY_DELAY_SEC", base.delay_sec))
        )
        if stall_retry_cli is not None:
            stall_retry = stall_retry_cli
        else:
            env_stall = os.environ.get("RIPKNIFE_STALL_RETRY")
            if env_stall is not None:
                stall_retry = env_stall.strip().lower() in ("1", "true", "yes", "on")
            else:
                stall_retry = base.stall_retry
        nas_retries = int(os.environ.get("RIPKNIFE_NAS_RETRIES", base.nas_retries))
        return cls(
            count=max(0, count),
            delay_sec=max(0.0, delay),
            stall_retry=stall_retry,
            nas_retries=max(0, nas_retries),
        )


def backoff_seconds(attempt: int, delay_sec: float) -> float:
    """Fixed delay between attempts (attempt index kept for API clarity)."""
    _ = attempt
    return delay_sec


def title_should_retry(
    exc: BaseException,
    *,
    sector_retries_used: int,
    stall_retry: bool,
) -> bool:
    """One in-rip retry for a bad sector. Tray and recovery are not that retry.

    A stall that is not a bad sector still uses ``stall_retry``. An empty
    tray waits for one reinsert instead of counting as a sector retry.
    """
    from ripknife.recover import (
        FAILURE_RETRY_NORMAL,
        FAILURE_STOP,
        decide_failure_action,
    )

    action = decide_failure_action(str(exc), normal_retries_used=sector_retries_used)
    if action == FAILURE_RETRY_NORMAL:
        return sector_retries_used < 1
    if action == FAILURE_STOP and stall_retry and isinstance(exc, RipStallError):
        return True
    return False


def is_disc_retry_exhaustion_failure(exc: BaseException) -> bool:
    """True when failure is a disc-open / stall / missing-MKV case that needs intervention."""
    if isinstance(exc, (DiscOpenError, RipStallError)):
        return True
    if isinstance(exc, MakeMkvProcessError) and (
        exc.exit_code == 11 or "Failed to open disc" in str(exc)
    ):
        return True
    if isinstance(exc, RuntimeError) and "No MKV produced" in str(exc):
        return True
    return False


def alert_retries_exhausted_and_eject(
    device: str,
    exc: BaseException,
    *,
    label: str,
    retries: int = DEFAULT_RETRY_COUNT,
) -> bool:
    """Log user-intervention ALERT and eject the disc after retries are exhausted.

    Call only on final failure — never between retries (ejecting mid-retry would
    remove the disc for subsequent attempts). Returns whether eject succeeded.
    """
    from ripknife.nas import eject_disc
    from ripknife.paths import disc_dev

    total = max(0, retries) + 1
    logger.error(
        "%s USER INTERVENTION REQUIRED — %s failed after all retries "
        "(%d retries / %d total attempts)",
        ALERT_PREFIX,
        label,
        max(0, retries),
        total,
    )
    logger.error("%s Last error: %s", ALERT_PREFIX, exc)
    logger.error(
        "%s Action: clean or re-seat the disc, re-insert it, then re-run the rip "
        "(TV: use --start-at N to resume from the failed slot)",
        ALERT_PREFIX,
    )
    ejected = eject_disc(disc_dev(device))
    if ejected:
        logger.error("%s Disc ejected for inspection — re-insert when ready", ALERT_PREFIX)
    else:
        logger.error(
            "%s Eject failed — eject manually, then clean/re-seat/re-insert and re-run",
            ALERT_PREFIX,
        )
    return ejected


def call_with_retries(
    fn: Callable[[], T],
    *,
    retries: int,
    delay_sec: float,
    label: str,
    should_retry: Callable[[BaseException], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` up to ``retries + 1`` times.

    ``retries`` is the number of *extra* attempts after the first failure
    (3 = three retries = 4 total attempts). Does not eject between attempts.
    """
    attempts = max(0, retries) + 1
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except BaseException as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            if should_retry is not None and not should_retry(exc):
                raise
            wait = backoff_seconds(attempt, delay_sec)
            logger.warning(
                "%s RETRY %d/%d — %s failed; waiting %ss before retry: %s",
                ALERT_PREFIX,
                attempt,
                retries,
                label,
                int(wait) if wait == int(wait) else wait,
                exc,
            )
            if wait > 0:
                sleep(wait)
    assert last_exc is not None
    raise last_exc

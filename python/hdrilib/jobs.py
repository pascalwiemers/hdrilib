"""Shared bounded parallel-job coordinator."""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Iterable, TypeVar


Result = TypeVar("Result")


class JobCancelled(RuntimeError):
    """Raised by a worker when a shared job cancellation event is set."""


def run_parallel(
    items: Iterable[str],
    worker: Callable[[str, threading.Event], Result],
    workers: int = 1,
    cancel_event: threading.Event | None = None,
    on_result: Callable[[str, Result], None] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    thread_name_prefix: str = "hdrilib-job",
) -> tuple[int, int, bool]:
    """Run a bounded concurrent job and invoke callbacks on the calling thread.

    Only enough futures to occupy the pool are submitted at once. Cancellation
    therefore stops queued work promptly, while the shared event lets active workers
    terminate their subprocesses. Failures count as completed work.
    """

    values = list(items)
    total = len(values)
    if not total:
        return 0, 0, bool(cancel_event and cancel_event.is_set())

    event = cancel_event or threading.Event()
    worker_count = max(1, min(64, int(workers)))
    executor = ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix=thread_name_prefix
    )
    value_iterator = iter(values)
    futures = {}

    def submit_one():
        try:
            value = next(value_iterator)
        except StopIteration:
            return None
        future = executor.submit(worker, value, event)
        futures[future] = value
        return future

    for _index in range(min(worker_count, total)):
        submit_one()
    pending = set(futures)
    completed = 0
    try:
        while pending:
            if event.is_set():
                for future in pending:
                    future.cancel()
            done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                value = futures[future]
                if future.cancelled():
                    continue
                try:
                    result = future.result()
                except JobCancelled:
                    continue
                except Exception as error:
                    completed += 1
                    if on_error is not None:
                        on_error(value, error)
                else:
                    completed += 1
                    if on_result is not None:
                        on_result(value, result)
                if on_progress is not None:
                    on_progress(completed, total)
                if not event.is_set():
                    replacement = submit_one()
                    if replacement is not None:
                        pending.add(replacement)
    finally:
        if event.is_set():
            for future in futures:
                future.cancel()
        executor.shutdown(wait=True)
    return completed, total, event.is_set()

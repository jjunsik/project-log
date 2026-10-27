"""Optional Gemini 3.5 Free quota reservations; no credentials, prompts or product DB.

Remove this module and its two adapter/child hooks to restore unpaced calls. State is
local to ONE Google project/model; other clients are invisible. Reservations survive
failures/restarts and are never refunded for an uncertain transmission.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from project_log.gemini import error_details
from project_log.git import CollectionError

RPM, TPM, RPD = 15, 250_000, 500
PACIFIC = ZoneInfo("America/Los_Angeles")
STATE_LIMIT = 64 * 1024
QUOTA_WAIT_SECONDS = 120


class MinuteProviderError(CollectionError):
    """Explicit minute quota refusal; one shared retry budget with HTTP503."""

    def __init__(self, delay: float = 60):
        super().__init__("provider_minute_limit", "분당 요청·토큰 한도에 도달했습니다.")
        self.delay = delay


def from_environment(*, stopped: threading.Event) -> FreeTierQuota | None:
    if os.environ.get("PROJECT_LOG_GEMINI_FREE_LIMITS") != "1":
        return None
    filename = os.environ.get("PROJECT_LOG_GEMINI_FREE_LIMITS_STATE", "")
    if os.environ.get("PROJECT_LOG_GEMINI_MODEL") != "gemini-3.5-flash-lite" or not filename:
        raise CollectionError(
            "gemini_free_configuration", "무료 호출 제어 모델·상태 경로가 필요합니다."
        )
    path = Path(filename)
    if not path.is_absolute():
        raise CollectionError("gemini_free_configuration", "호출 제어 상태는 절대 경로여야 합니다.")
    return FreeTierQuota(path, stopped=stopped)


class FreeTierQuota:
    def __init__(
        self,
        path: Path,
        *,
        stopped: threading.Event | None = None,
        clock: Callable[[], float] = time.time,
        wait: Callable[[float], bool] | None = None,
    ):
        self.path = path
        self.stopped = stopped or threading.Event()
        self.clock, self.wait = clock, wait or self.stopped.wait

    def check(self) -> None:
        if self.stopped.is_set():
            raise CollectionError("analysis_stopped", "분석 실행이 중단됐습니다.")

    def pause(self, seconds: float) -> None:
        self.check()
        if self.wait(seconds):
            raise CollectionError("analysis_stopped", "분석 실행이 중단됐습니다.")
        self.check()

    @contextmanager
    def locked(self, *, deadline: float | None = None) -> Iterator[bool]:
        """Stable sidecar lock; a missing ledger after initialization is fail-closed."""
        fd = -1
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock = self.path.with_name(self.path.name + ".lock")
            try:
                fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
            self.private(fd)
            deadline = deadline if deadline is not None else time.monotonic() + QUOTA_WAIT_SECONDS
            while True:
                self.check()
                if time.monotonic() >= deadline:
                    raise CollectionError("provider_minute_limit", "호출량 잠금 대기 한도입니다.")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.pause(0.1)
            # Check initialization under the lock, not at O_CREAT: another process
            # can acquire the new sidecar before its creator does.
            fresh = os.fstat(fd).st_size == 0
            yield fresh
            if fresh and self.path.exists():
                os.write(fd, b"1")
                os.fsync(fd)
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            raise CollectionError(
                "gemini_free_state_unavailable", "무료 호출량 상태를 확인·보존할 수 없습니다."
            ) from None
        finally:
            if fd >= 0:
                os.close(fd)

    @staticmethod
    def private(fd: int) -> None:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > STATE_LIMIT
        ):
            raise ValueError("Invalid local quota state")

    def load(self, now: float, fresh: bool) -> dict[str, Any]:
        day = datetime.fromtimestamp(now, PACIFIC).date().isoformat()
        if not math.isfinite(now):
            raise ValueError("Invalid clock")
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if not fresh:
                raise ValueError("Quota ledger missing") from None
            return dict(version=1, day=day, count=0, last_time=now, recent=[], blocked=False)
        with os.fdopen(fd, "rb") as stream:
            self.private(stream.fileno())
            state: dict[str, Any] = json.loads(stream.read(STATE_LIMIT + 1))
        if (
            set(state) != {"version", "day", "count", "last_time", "recent", "blocked"}
            or state["version"] != 1
            or type(state["count"]) is not int
            or not 0 <= state["count"] <= RPD
            or type(state["blocked"]) is not bool
            or type(state["last_time"]) not in (int, float)
            or not math.isfinite(state["last_time"])
            or now < state["last_time"]
            or not isinstance(state["day"], str)
            or state["day"]
            != datetime.fromtimestamp(state["last_time"], PACIFIC).date().isoformat()
            or not isinstance(state["recent"], list)
            or len(state["recent"]) > RPM
        ):
            raise ValueError("Invalid quota state or regressed clock")
        for entry in state["recent"]:
            if (
                not isinstance(entry, list)
                or len(entry) != 3
                or type(entry[0]) not in (int, float)
                or not math.isfinite(entry[0])
                or not 0 <= entry[0] <= state["last_time"]
                or type(entry[1]) is not int
                or entry[1] < 0
                or not isinstance(entry[2], str)
                or len(entry[2]) != 32
            ):
                raise ValueError("Invalid reservation")
        if state["day"] != day:
            state.update(day=day, count=0, blocked=False)
        state["recent"] = [e for e in state["recent"] if now - e[0] < 60]
        state["last_time"] = now
        return state

    def save(self, state: dict[str, Any]) -> None:
        name = ""
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.path.parent, prefix=".gemini-quota-", delete=False
            ) as stream:
                name = stream.name
                stream.write(json.dumps(state, allow_nan=False).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if name:
                Path(name).unlink(missing_ok=True)

    @staticmethod
    def estimate(raw: bytes) -> int:
        # Whole serialized SDK request includes text, instructions, schema and wrappers.
        # One UTF-8 wire byte/token + framing margin is deliberately NOT chars/4.
        # This is an estimate, not a tokenizer guarantee. Native media has no safe
        # byte/token relationship: stop explicitly rather than silently omit it.
        body = json.loads(raw)
        if any("inlineData" in p for c in body["contents"] for p in c["parts"]):
            raise CollectionError(
                "gemini_free_tokens_unknown",
                "무료 호출 제어에서 비텍스트 토큰을 사전 산정할 수 없습니다.",
            )
        tokens = len(raw) + 1024
        if tokens > TPM:
            raise CollectionError(
                "gemini_free_request_limit", "단일 요청의 보수적 입력 토큰 추정이 TPM을 초과합니다."
            )
        return tokens

    def reserve(self, raw: bytes, gate: Callable[[], None]) -> str:
        tokens = self.estimate(raw)
        started, waited = time.monotonic(), 0.0
        while True:
            with self.locked(deadline=started + QUOTA_WAIT_SECONDS) as fresh:
                now = self.clock()
                state = self.load(now, fresh)
                if state["blocked"] or state["count"] >= RPD:
                    raise CollectionError(
                        "gemini_free_daily_limit",
                        "무료 일일 호출 한도를 소진했습니다. 자동 재개하지 않습니다.",
                    )
                recent = state["recent"]
                remaining = recent.copy()
                while remaining and (
                    len(remaining) >= RPM or sum(e[1] for e in remaining) + tokens > TPM
                ):
                    remaining.pop(0)
                delay = max((e[0] + 60 - now for e in recent if e not in remaining), default=0)
                if delay <= 0:
                    self.check()
                    gate()  # Existing durable worker entry only AFTER quota waits.
                    self.check()
                    now = self.clock()
                    state = self.load(now, fresh)
                    reservation = uuid4().hex
                    state["count"] += 1
                    state["recent"].append([now, tokens, reservation])
                    self.save(state)  # fsync BEFORE entering HTTP; never refund on failure.
                    return reservation
                if max(time.monotonic() - started, waited) + delay > QUOTA_WAIT_SECONDS:
                    raise CollectionError("provider_minute_limit", "분당 호출량 대기 한도입니다.")
                # Persist initialization even when no request is sent.
                self.save(state)
            waited += delay
            self.pause(delay)

    def usage(self, reservation: str, tokens: Any) -> None:
        if type(tokens) is not int or tokens < 0:
            return  # Missing/unusable metadata retains the conservative reservation.
        with self.locked() as fresh:
            state = self.load(self.clock(), fresh)
            for entry in state["recent"]:
                if entry[2] == reservation:
                    entry[1] = tokens
            self.save(state)

    def reject_429(self, response: httpx.Response) -> None:
        """Only explicit minute quota is retryable; daily and unknown fail closed."""
        daily, minute, delay = False, False, 60.0
        try:
            details = error_details(response, STATE_LIMIT)["details"]
            ids = [
                v.get("quotaId", "").lower().replace("_", "")
                for d in details
                if d.get("@type")
                in {"type.googleapis.com/google.rpc.QuotaFailure", "google.rpc.QuotaFailure"}
                for v in d.get("violations", [])
            ]
            daily = any("perday" in key for key in ids)
            minute = bool(ids) and all("perminute" in key for key in ids)
            hints = [
                str(d.get("retryDelay", ""))
                for d in details
                if d.get("@type")
                in {"type.googleapis.com/google.rpc.RetryInfo", "google.rpc.RetryInfo"}
            ]
            hints += [response.headers.get("retry-after", "") + "s"]
            seconds = [
                min(float(h[:-1]), QUOTA_WAIT_SECONDS + 1.0)
                for h in hints
                if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?s", h)
            ]
            if seconds:
                delay = max(1.0, *seconds)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            pass
        if daily:
            with self.locked() as fresh:
                state = self.load(self.clock(), fresh)
                state["blocked"] = True
                self.save(state)
            raise CollectionError(
                "gemini_free_daily_limit",
                "Google이 일일 호출 한도 소진을 알렸습니다. 자동 재개하지 않습니다.",
            )
        if minute:
            raise MinuteProviderError(delay)
        raise CollectionError(
            "provider_rate_limited",
            "Gemini 호출 한도 오류입니다. 분당·일일·공유 한도가 가능하며 자동 재전송하지 않습니다.",
        )

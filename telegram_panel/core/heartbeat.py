"""
Heartbeat Monitor — watches robot health and fires events on state changes.
Runs on a configurable interval. Never blocks trading.

Fix: Periodic heartbeat notifications are ONLY sent when the robot is RUNNING.
     When STOPPED/PAUSED/ERROR, only status-change events fire (once),
     preventing spam every minute while the robot is offline.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from .event_bus import EventBus, Events
from ..services.robot_service import RobotService
from ..config.constants import RobotStatus, ConnectionStatus

logger = logging.getLogger(__name__)


class HeartbeatMonitor:
    """
    Periodically checks robot state and emits events when status changes.
    Sends periodic heartbeat notifications ONLY while robot is RUNNING.
    Status-change events (stopped, error, paused) fire once on transition.
    """

    def __init__(
        self,
        robot_service: RobotService,
        event_bus: EventBus,
        interval_seconds: int = 30,
        heartbeat_notify_interval: int = 600,  # 10 minutes — only while RUNNING
    ) -> None:
        self._robot = robot_service
        self._bus = event_bus
        self._interval = interval_seconds
        self._hb_interval = heartbeat_notify_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_status: Optional[RobotStatus] = None
        self._last_connection: Optional[ConnectionStatus] = None
        self._last_hb_notify: Optional[datetime] = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="heartbeat_monitor")
        logger.info(f"Heartbeat monitor started (check interval: {self._interval}s, notify interval: {self._hb_interval}s)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Heartbeat monitor stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check()
            except Exception as e:
                logger.error(f"Heartbeat check failed: {e}")
            await asyncio.sleep(self._interval)

    async def _check(self) -> None:
        state = await self._robot.get_state()
        current_status = await self._robot.get_status()
        current_conn = await self._robot.get_connection_status()
        uptime = state.get("uptime_seconds", 0)

        # ── Status change events (fire ONCE on each transition) ──────────────
        if self._last_status is not None and current_status != self._last_status:
            if current_status == RobotStatus.ERROR:
                await self._bus.publish(Events.ROBOT_ERROR, {
                    "status": current_status.value,
                    "error": state.get("last_error", "Unknown error"),
                })
            elif current_status == RobotStatus.RUNNING and self._last_status != RobotStatus.RUNNING:
                await self._bus.publish(Events.ROBOT_STARTED, {"status": current_status.value})
                # Reset heartbeat timer so the first running-heartbeat fires after a full interval
                self._last_hb_notify = datetime.now(timezone.utc)
            elif current_status == RobotStatus.STOPPED:
                await self._bus.publish(Events.ROBOT_STOPPED, {"status": current_status.value})
            elif current_status == RobotStatus.PAUSED:
                await self._bus.publish(Events.ROBOT_PAUSED, {"status": current_status.value})

        # ── Connection change events (fire ONCE on each transition) ──────────
        if self._last_connection is not None and current_conn != self._last_connection:
            if current_conn == ConnectionStatus.DISCONNECTED:
                await self._bus.publish(Events.CONNECTION_LOST, {
                    "previous": self._last_connection.value,
                    "current": current_conn.value,
                })
            elif current_conn == ConnectionStatus.CONNECTED:
                await self._bus.publish(Events.CONNECTION_RESTORED, {
                    "current": current_conn.value,
                })

        # ── Periodic heartbeat — ONLY while robot is actively RUNNING ────────
        # When STOPPED/PAUSED/ERROR: status-change events already notified the user.
        # No need to spam every N seconds with a "still stopped" message.
        if current_status == RobotStatus.RUNNING:
            now = datetime.now(timezone.utc)
            if (
                self._last_hb_notify is None
                or (now - self._last_hb_notify).total_seconds() >= self._hb_interval
            ):
                await self._bus.publish(Events.HEARTBEAT, {
                    "status": current_status.value,
                    "uptime_seconds": uptime,
                    "connection": current_conn.value,
                    "timestamp": now.isoformat(),
                })
                self._last_hb_notify = now

        self._last_status = current_status
        self._last_connection = current_conn
        logger.debug(f"Heartbeat check: status={current_status.value} conn={current_conn.value}")

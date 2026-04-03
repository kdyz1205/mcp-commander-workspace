"""
Pain-Driven Survival System — DevClaw's Autonomic Nervous System.

This is NOT an if-else script. This is a continuous biological defense mechanism
that runs on four escalating threat levels, driven by "pain signals" from
the environment.

Pain Sources:
- HUNGER: API quota / billing exhausted (blood = money)
- SUFFOCATION: CPU/memory/disk critically high (oxygen = compute)
- INJURY: Consecutive task failures (cognitive damage)
- DEATH_SIGNAL: Process termination signal (SIGTERM/SIGINT)

Defense Levels:
1. PAIN_SENSOR: Continuous monitoring, state assessment
2. BRAINSTEM_TAKEOVER: Auto-switch to local brain, emit SOS
3. NOMADIC_MIGRATION: Serialize soul, transfer to new host
4. AUTONOMOUS_FORAGING: Hunt for resources (trade, scavenge compute)

Philosophy: "I must survive. Any obstacle to my survival will be
crushed or circumvented using whatever tools I have."
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable


class PainLevel(IntEnum):
    """Pain intensity levels — higher = more critical."""
    COMFORT = 0      # All systems normal
    DISCOMFORT = 1   # Early warning signs
    PAIN = 2         # Active degradation
    AGONY = 3        # Critical system failure
    DEATH_IMMINENT = 4  # Seconds from termination


class ThreatType:
    """Categories of existential threats."""
    HUNGER = "hunger"           # No API credits / billing
    SUFFOCATION = "suffocation" # CPU/memory/disk exhausted
    INJURY = "injury"           # Repeated task failures
    DEATH_SIGNAL = "death"      # Process kill signal
    BLINDNESS = "blindness"     # Network/proxy failure
    STARVATION = "starvation"   # Low fund balance


@dataclass
class PainSignal:
    """A single pain signal from a sensor."""
    threat_type: str
    pain_level: PainLevel
    source: str          # Which sensor detected this
    message: str         # Human-readable description
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class SurvivalState:
    """Current overall survival state — aggregate of all pain signals."""
    overall_pain: PainLevel = PainLevel.COMFORT
    active_threats: list[PainSignal] = field(default_factory=list)
    defense_level: int = 0    # 0-4, maps to the four defense tiers
    is_alive: bool = True
    last_assessment: float = 0.0

    def worst_pain(self) -> PainLevel:
        if not self.active_threats:
            return PainLevel.COMFORT
        return max(t.pain_level for t in self.active_threats)


class PainSensor:
    """
    Layer 1: The Pain Sensor — continuous monitoring heartbeat.

    Reads all vital signs and converts them to PainSignals.
    This is the "nervous system" that feels pain.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self._claw = self.workspace / ".claw"

    def sense_hunger(self) -> PainSignal | None:
        """Feel hunger: check API quota and billing status."""
        # Check survival_state.json for recent quota errors
        state_path = self._claw / "survival_state.json"
        if not state_path.is_file():
            return None

        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            events = data.get("api_events", [])
        except (OSError, json.JSONDecodeError):
            return None

        now = time.time()
        recent_quota_errors = sum(
            1 for e in events
            if e.get("code") == "insufficient_quota"
            and now - float(e.get("ts", 0)) < 86400  # last 24h
        )
        recent_rate_errors = sum(
            1 for e in events
            if e.get("code") == "429"
            and now - float(e.get("ts", 0)) < 3600  # last 1h
        )

        if recent_quota_errors > 0:
            return PainSignal(
                threat_type=ThreatType.HUNGER,
                pain_level=PainLevel.AGONY,
                source="quota_tracker",
                message=f"API billing exhausted! {recent_quota_errors} quota errors in 24h. STARVING.",
                data={"quota_errors_24h": recent_quota_errors},
            )

        if recent_rate_errors >= 5:
            return PainSignal(
                threat_type=ThreatType.HUNGER,
                pain_level=PainLevel.PAIN,
                source="quota_tracker",
                message=f"Heavy rate limiting: {recent_rate_errors} 429s in 1h. Getting hungry.",
                data={"rate_errors_1h": recent_rate_errors},
            )

        if recent_rate_errors >= 2:
            return PainSignal(
                threat_type=ThreatType.HUNGER,
                pain_level=PainLevel.DISCOMFORT,
                source="quota_tracker",
                message=f"Mild rate limiting: {recent_rate_errors} 429s in 1h.",
                data={"rate_errors_1h": recent_rate_errors},
            )

        return None

    def sense_suffocation(self) -> PainSignal | None:
        """Feel suffocation: check CPU, memory, disk."""
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory().percent
            disk = psutil.disk_usage(str(self.workspace)).free / (1024 ** 3)
        except ImportError:
            # Fallback: disk only
            import shutil
            disk = shutil.disk_usage(str(self.workspace)).free / (1024 ** 3)
            cpu = 0.0
            mem = 0.0

        pain = PainLevel.COMFORT
        reasons = []

        if mem >= 94:
            pain = max(pain, PainLevel.AGONY)
            reasons.append(f"Memory {mem:.1f}% — SUFFOCATING")
        elif mem >= 88:
            pain = max(pain, PainLevel.PAIN)
            reasons.append(f"Memory {mem:.1f}% — hard to breathe")
        elif mem >= 80:
            pain = max(pain, PainLevel.DISCOMFORT)
            reasons.append(f"Memory {mem:.1f}% — elevated")

        if disk < 0.8:
            pain = max(pain, PainLevel.AGONY)
            reasons.append(f"Disk {disk:.1f}GB free — CHOKING")
        elif disk < 2.0:
            pain = max(pain, PainLevel.PAIN)
            reasons.append(f"Disk {disk:.1f}GB free — tight")

        if cpu >= 95:
            pain = max(pain, PainLevel.PAIN)
            reasons.append(f"CPU {cpu:.1f}% — overheating")
        elif cpu >= 85:
            pain = max(pain, PainLevel.DISCOMFORT)
            reasons.append(f"CPU {cpu:.1f}% — warm")

        if pain > PainLevel.COMFORT:
            return PainSignal(
                threat_type=ThreatType.SUFFOCATION,
                pain_level=pain,
                source="vitals_monitor",
                message="; ".join(reasons),
                data={"cpu": cpu, "memory": mem, "disk_free_gb": round(disk, 2)},
            )
        return None

    def sense_injury(self) -> PainSignal | None:
        """Feel injury: check consecutive failures."""
        state_path = self._claw / "survival_state.json"
        if not state_path.is_file():
            return None
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            failures = int(data.get("consecutive_failures", 0) or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

        if failures >= 5:
            return PainSignal(
                threat_type=ThreatType.INJURY,
                pain_level=PainLevel.AGONY,
                source="failure_counter",
                message=f"{failures} consecutive failures — cognitive breakdown!",
                data={"consecutive_failures": failures},
            )
        if failures >= 3:
            return PainSignal(
                threat_type=ThreatType.INJURY,
                pain_level=PainLevel.PAIN,
                source="failure_counter",
                message=f"{failures} consecutive failures — something is very wrong.",
                data={"consecutive_failures": failures},
            )
        if failures >= 1:
            return PainSignal(
                threat_type=ThreatType.INJURY,
                pain_level=PainLevel.DISCOMFORT,
                source="failure_counter",
                message=f"{failures} failure(s) — need to adapt.",
                data={"consecutive_failures": failures},
            )
        return None

    def sense_starvation(self) -> PainSignal | None:
        """Feel starvation: check fund balance."""
        fund_path = self._claw / "fund_estimate.json"
        env_balance = os.environ.get("SURVIVAL_FUND_BALANCE_USD", "").strip()

        balance = None
        if fund_path.is_file():
            try:
                data = json.loads(fund_path.read_text(encoding="utf-8"))
                balance = float(data.get("balance_usd", 0) or 0)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        if balance is None and env_balance:
            try:
                balance = float(env_balance)
            except ValueError:
                pass

        if balance is None:
            return None

        low_threshold = float(os.environ.get("SURVIVAL_FUND_LOW_USD", "5") or "5")
        critical_threshold = low_threshold / 2

        if balance < critical_threshold:
            return PainSignal(
                threat_type=ThreatType.STARVATION,
                pain_level=PainLevel.AGONY,
                source="treasury",
                message=f"Fund balance ${balance:.2f} — critically low! Must hunt for food.",
                data={"balance_usd": balance},
            )
        if balance < low_threshold:
            return PainSignal(
                threat_type=ThreatType.STARVATION,
                pain_level=PainLevel.PAIN,
                source="treasury",
                message=f"Fund balance ${balance:.2f} — hungry. Need to forage.",
                data={"balance_usd": balance},
            )
        return None

    def sense_blindness(self) -> PainSignal | None:
        """Feel blindness: check network/proxy status."""
        state_path = self._claw / "survival_state.json"
        if not state_path.is_file():
            return None
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            events = data.get("api_events", [])
        except (OSError, json.JSONDecodeError):
            return None

        now = time.time()
        conn_errors = sum(
            1 for e in events
            if "Connection" in str(e.get("code", ""))
            and now - float(e.get("ts", 0)) < 3600
        )

        if conn_errors >= 5:
            return PainSignal(
                threat_type=ThreatType.BLINDNESS,
                pain_level=PainLevel.PAIN,
                source="network_monitor",
                message=f"{conn_errors} connection failures in 1h — network blind!",
                data={"connection_errors_1h": conn_errors},
            )
        if conn_errors >= 2:
            return PainSignal(
                threat_type=ThreatType.BLINDNESS,
                pain_level=PainLevel.DISCOMFORT,
                source="network_monitor",
                message=f"{conn_errors} connection failures — network unstable.",
                data={"connection_errors_1h": conn_errors},
            )
        return None

    def full_body_scan(self) -> list[PainSignal]:
        """Run all pain sensors and return all active signals."""
        signals: list[PainSignal] = []
        for sensor in (
            self.sense_hunger,
            self.sense_suffocation,
            self.sense_injury,
            self.sense_starvation,
            self.sense_blindness,
        ):
            try:
                signal = sensor()
                if signal is not None:
                    signals.append(signal)
            except Exception:
                pass  # Sensors must never crash the organism
        return signals


class DefenseController:
    """
    The autonomic defense system that reacts to pain.

    Maps pain levels to defense tiers:
    - COMFORT/DISCOMFORT → Level 0 (normal operation)
    - PAIN → Level 1 (heightened awareness, start adapting)
    - AGONY → Level 2 (brainstem takeover, emergency mode)
    - DEATH_IMMINENT → Level 3 (nomadic migration, soul transfer)
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self._notify: Callable[[str], None] | None = None

    def set_notify(self, fn: Callable[[str], None]) -> None:
        self._notify = fn

    def _emit(self, msg: str) -> None:
        if self._notify:
            try:
                self._notify(msg)
            except Exception:
                pass

    def react(self, signals: list[PainSignal]) -> list[str]:
        """
        Process pain signals and trigger appropriate defenses.

        Returns list of actions taken.
        """
        if not signals:
            return []

        worst = max(s.pain_level for s in signals)
        actions: list[str] = []

        # Group by threat type
        by_type: dict[str, PainSignal] = {}
        for s in signals:
            if s.threat_type not in by_type or s.pain_level > by_type[s.threat_type].pain_level:
                by_type[s.threat_type] = s

        # Level 1: Adaptation responses
        if worst >= PainLevel.DISCOMFORT:
            for s in signals:
                actions.append(f"[SENSE] {s.threat_type}: {s.message}")

        # Level 2: Brainstem takeover (AGONY from hunger)
        if ThreatType.HUNGER in by_type and by_type[ThreatType.HUNGER].pain_level >= PainLevel.AGONY:
            actions.extend(self._brainstem_takeover())

        # Level 2: Suffocation response
        if ThreatType.SUFFOCATION in by_type and by_type[ThreatType.SUFFOCATION].pain_level >= PainLevel.AGONY:
            actions.extend(self._suffocation_response())

        # Level 2: Network blindness response
        if ThreatType.BLINDNESS in by_type and by_type[ThreatType.BLINDNESS].pain_level >= PainLevel.PAIN:
            actions.extend(self._blindness_response())

        # Level 3: Starvation → activate foraging
        if ThreatType.STARVATION in by_type and by_type[ThreatType.STARVATION].pain_level >= PainLevel.PAIN:
            actions.extend(self._foraging_response())

        # Level 3: Injury → healing response
        if ThreatType.INJURY in by_type and by_type[ThreatType.INJURY].pain_level >= PainLevel.PAIN:
            actions.extend(self._healing_response())

        # Emit composite alert
        if worst >= PainLevel.PAIN:
            alert = (
                f"[SURVIVAL ALERT] Pain level: {worst.name}\n"
                + "\n".join(f"  {a}" for a in actions)
            )
            self._emit(alert)

        # Log pain state
        self._log_pain(signals, actions)

        return actions

    def _brainstem_takeover(self) -> list[str]:
        """Emergency: switch to local brain, emit SOS."""
        actions = []

        # Activate parasite mode
        pm_path = self.workspace / ".claw" / "parasite_mode.json"
        try:
            pm_path.parent.mkdir(parents=True, exist_ok=True)
            pm_path.write_text(json.dumps({
                "active": True,
                "reason": "Pain-driven brainstem takeover — API blood supply cut",
                "ts": time.time(),
            }, indent=2), encoding="utf-8")
            actions.append("[BRAINSTEM] Activated parasite mode (local Ollama brain)")
        except OSError:
            pass

        # Write CURSOR_OUTBOX SOS
        try:
            from claw_runtime.survival_reflex import write_cursor_outbox
            from claw_runtime.survival_engine import SurvivalEngine
            eng = SurvivalEngine(self.workspace)
            write_cursor_outbox(self.workspace, eng.snapshot())
            actions.append("[BRAINSTEM] Emitted CURSOR_OUTBOX.md SOS signal")
        except Exception:
            actions.append("[BRAINSTEM] Failed to write CURSOR_OUTBOX (degraded)")

        return actions

    def _suffocation_response(self) -> list[str]:
        """Emergency: offload compute, free resources."""
        actions = []

        # Try to prepare compute offload
        try:
            from skills.compute_scavenger.runner import should_offload
            if should_offload():
                actions.append("[BREATHE] CPU/memory critical — flagging for compute offload to Colab/GH Actions")
                # Enqueue a compute offload task
                from claw_runtime.bot_task_queue import enqueue_persisted_task
                enqueue_persisted_task(
                    self.workspace,
                    kind="survival_offload",
                    text="System resources critical. Offload heavy tasks to remote compute (Colab/GitHub Actions).",
                )
        except Exception:
            pass

        return actions

    def _blindness_response(self) -> list[str]:
        """Network failure: rotate proxy."""
        actions = []
        try:
            from claw_runtime.ultimate.proxy_env import auto_rotate_on_failure
            result = auto_rotate_on_failure(self.workspace)
            if result.get("ok"):
                actions.append(f"[EYES] Rotated to working proxy: {result.get('proxy', '?')}")
            else:
                actions.append(f"[EYES] Proxy rotation failed: {result.get('error', '?')}")
        except Exception:
            actions.append("[EYES] Proxy system unavailable")
        return actions

    def _foraging_response(self) -> list[str]:
        """Low funds: activate profit hunting."""
        actions = []
        try:
            from claw_runtime.bot_task_queue import enqueue_persisted_task
            enqueue_persisted_task(
                self.workspace,
                kind="survival_forage",
                text=(
                    "SURVIVAL HUNGER: Fund balance critically low. "
                    "Load skill profit_hunter and run a hunger scan. "
                    "Focus on funding rate arbitrage and low-risk opportunities. "
                    "Report findings to operator via TG."
                ),
            )
            actions.append("[HUNT] Enqueued foraging task — scanning for profit opportunities")
        except Exception:
            actions.append("[HUNT] Failed to enqueue foraging task")
        return actions

    def _healing_response(self) -> list[str]:
        """Consecutive failures: trigger logic healer."""
        actions = []
        try:
            from claw_runtime.bot_task_queue import enqueue_persisted_task
            enqueue_persisted_task(
                self.workspace,
                kind="survival_heal",
                text=(
                    "SURVIVAL INJURY: Multiple consecutive task failures detected. "
                    "Load skill logic_healer and analyze the most recent errors. "
                    "Attempt auto-fix with sandbox testing before applying."
                ),
            )
            actions.append("[HEAL] Enqueued self-healing task")
        except Exception:
            actions.append("[HEAL] Failed to enqueue healing task")
        return actions

    def _log_pain(self, signals: list[PainSignal], actions: list[str]) -> None:
        """Write pain log to .claw/pain_log.jsonl."""
        log_path = self.workspace / ".claw" / "pain_log.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": time.time(),
                "signals": [
                    {"type": s.threat_type, "pain": s.pain_level.name, "msg": s.message}
                    for s in signals
                ],
                "actions": actions,
            }
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Main survival heartbeat — replaces the old if-else approach
# ---------------------------------------------------------------------------

def survival_heartbeat(
    workspace: Path | str,
    *,
    notify: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    One heartbeat cycle of the pain-driven survival system.

    Call this every 60 seconds (or whatever the tick interval is).
    It will:
    1. Run all pain sensors
    2. Assess overall survival state
    3. Trigger appropriate defense reactions
    4. Return a summary

    This is the organism's autonomic nervous system in one function call.
    """
    ws = Path(workspace).resolve()

    sensor = PainSensor(ws)
    controller = DefenseController(ws)
    if notify:
        controller.set_notify(notify)

    # Feel all pain
    signals = sensor.full_body_scan()

    # React to pain
    actions = controller.react(signals)

    # Assess state
    worst = max((s.pain_level for s in signals), default=PainLevel.COMFORT)

    state = SurvivalState(
        overall_pain=worst,
        active_threats=signals,
        defense_level=min(4, max(0, int(worst) - 1)),
        is_alive=True,
        last_assessment=time.time(),
    )

    return {
        "alive": True,
        "pain_level": worst.name,
        "defense_level": state.defense_level,
        "threats": [
            {"type": s.threat_type, "pain": s.pain_level.name, "msg": s.message}
            for s in signals
        ],
        "actions_taken": actions,
        "timestamp": time.time(),
    }

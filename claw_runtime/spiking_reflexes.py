"""
Event-Driven Spiking Neural Reflexes — DevClaw's Spinal Cord.

The main brain sleeps to save compute. A lightweight "spinal cord"
watches for critical events and wakes the brain instantly.

Architecture:
- Each watcher is a "neuron" that fires when its threshold is crossed
- Spikes are routed through an asyncio queue to the on_spike callback
- The spinal cord runs in a background thread, consuming near-zero
  resources until a spike fires
- Price watchers connect to exchange WebSocket feeds (Binance/OKX)
- System vitals and file watchers use lightweight polling

Philosophy: "I sleep to conserve energy. But my reflexes never sleep."
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spike data types
# ---------------------------------------------------------------------------

@dataclass
class PriceSpike:
    """A price spike event from an exchange WebSocket."""
    symbol: str
    exchange: str
    old_price: float
    new_price: float
    change_pct: float
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        direction = "+" if self.change_pct > 0 else ""
        return (
            f"PriceSpike({self.symbol}@{self.exchange}: "
            f"${self.old_price:.2f} -> ${self.new_price:.2f} "
            f"[{direction}{self.change_pct:.2f}%])"
        )


@dataclass
class SystemSpike:
    """A system-level spike (memory, CPU, disk)."""
    metric: str          # "memory" | "cpu" | "disk"
    value: float         # current value (percent)
    threshold: float     # threshold that was crossed
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return (
            f"SystemSpike({self.metric}: {self.value:.1f}% "
            f"> threshold {self.threshold:.1f}%)"
        )


@dataclass
class FileSpike:
    """A file-change spike."""
    file_path: str
    old_mtime: float
    new_mtime: float
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return f"FileSpike({self.file_path} modified)"


@dataclass
class BalanceSpike:
    """A balance drop spike."""
    current_usd: float
    threshold_usd: float
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return (
            f"BalanceSpike(${self.current_usd:.2f} "
            f"< threshold ${self.threshold_usd:.2f})"
        )


# ---------------------------------------------------------------------------
# Watch definitions (neurons)
# ---------------------------------------------------------------------------

@dataclass
class PriceWatch:
    """Neuron: fires when price moves more than threshold_pct."""
    symbol: str
    exchange: str
    threshold_pct: float
    last_price: float | None = None


@dataclass
class FileWatch:
    """Neuron: fires when a file's mtime changes."""
    file_path: str
    last_mtime: float | None = None


@dataclass
class BalanceWatch:
    """Neuron: fires when balance drops below threshold."""
    threshold_usd: float


@dataclass
class MemoryWatch:
    """Neuron: fires when memory usage exceeds threshold."""
    threshold_pct: float = 94.0


# ---------------------------------------------------------------------------
# Async watchers
# ---------------------------------------------------------------------------

def _binance_ws_url(symbol: str) -> str:
    """Build Binance WebSocket ticker URL for a symbol."""
    # Binance expects lowercase: btcusdt, ethusdt, etc.
    s = symbol.lower().replace("/", "").replace("-", "").replace("_", "")
    return f"wss://stream.binance.com:9443/ws/{s}@ticker"


def _okx_ws_url() -> str:
    """OKX uses a single endpoint with subscription messages."""
    return "wss://ws.okx.com:8443/ws/v5/public"


async def watch_price_websocket(
    symbol: str,
    exchange: str,
    spike_queue: asyncio.Queue[Any],
    threshold_pct: float = 5.0,
) -> None:
    """
    Connect to exchange WebSocket for real-time price updates.

    When the price change exceeds threshold_pct from the initial price
    seen in this session, enqueue a PriceSpike.

    Supports Binance and OKX. Falls back to REST polling if websockets
    library is not installed.
    """
    try:
        import websockets  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "websockets library not installed — falling back to REST polling "
            "for %s@%s. Install with: pip install websockets",
            symbol, exchange,
        )
        await _poll_price_rest(symbol, exchange, spike_queue, threshold_pct)
        return

    anchor_price: float | None = None
    exchange_lower = exchange.lower()

    while True:
        try:
            if exchange_lower == "binance":
                url = _binance_ws_url(symbol)
                async with websockets.connect(url) as ws:
                    logger.info("Connected to Binance WS for %s", symbol)
                    async for raw_msg in ws:
                        data = json.loads(raw_msg)
                        price = float(data.get("c", 0))  # 'c' = last price
                        if price <= 0:
                            continue
                        if anchor_price is None:
                            anchor_price = price
                            continue
                        change_pct = ((price - anchor_price) / anchor_price) * 100
                        if abs(change_pct) >= threshold_pct:
                            spike = PriceSpike(
                                symbol=symbol,
                                exchange=exchange,
                                old_price=anchor_price,
                                new_price=price,
                                change_pct=change_pct,
                            )
                            await spike_queue.put(spike)
                            logger.warning("Price spike detected: %s", spike)
                            anchor_price = price  # reset anchor

            elif exchange_lower == "okx":
                url = _okx_ws_url()
                inst_id = symbol.upper().replace("/", "-")
                if "-" not in inst_id:
                    # e.g. "BTCUSDT" -> "BTC-USDT"
                    if inst_id.endswith("USDT"):
                        inst_id = inst_id[:-4] + "-USDT"
                    elif inst_id.endswith("USD"):
                        inst_id = inst_id[:-3] + "-USD"
                sub_msg = json.dumps({
                    "op": "subscribe",
                    "args": [{"channel": "tickers", "instId": inst_id}],
                })
                async with websockets.connect(url) as ws:
                    await ws.send(sub_msg)
                    logger.info("Connected to OKX WS for %s", inst_id)
                    async for raw_msg in ws:
                        data = json.loads(raw_msg)
                        if "data" not in data:
                            continue
                        for tick in data["data"]:
                            price = float(tick.get("last", 0))
                            if price <= 0:
                                continue
                            if anchor_price is None:
                                anchor_price = price
                                continue
                            change_pct = (
                                (price - anchor_price) / anchor_price
                            ) * 100
                            if abs(change_pct) >= threshold_pct:
                                spike = PriceSpike(
                                    symbol=symbol,
                                    exchange=exchange,
                                    old_price=anchor_price,
                                    new_price=price,
                                    change_pct=change_pct,
                                )
                                await spike_queue.put(spike)
                                logger.warning(
                                    "Price spike detected: %s", spike
                                )
                                anchor_price = price
            else:
                logger.error(
                    "Unsupported exchange %r for price watch", exchange
                )
                return

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "WebSocket error for %s@%s: %s — reconnecting in 10s",
                symbol, exchange, exc,
            )
            await asyncio.sleep(10)


async def _poll_price_rest(
    symbol: str,
    exchange: str,
    spike_queue: asyncio.Queue[Any],
    threshold_pct: float,
    interval: float = 15.0,
) -> None:
    """
    Fallback price polling via REST when websockets is not available.

    Uses urllib to avoid hard dependency on requests/httpx.
    """
    import urllib.request

    exchange_lower = exchange.lower()
    anchor_price: float | None = None

    while True:
        try:
            price: float | None = None

            if exchange_lower == "binance":
                s = symbol.upper().replace("/", "").replace("-", "")
                url = (
                    f"https://api.binance.com/api/v3/ticker/price?symbol={s}"
                )
                req = urllib.request.Request(url, headers={"User-Agent": "claw"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                    price = float(data.get("price", 0))

            elif exchange_lower == "okx":
                inst_id = symbol.upper().replace("/", "-")
                if "-" not in inst_id:
                    if inst_id.endswith("USDT"):
                        inst_id = inst_id[:-4] + "-USDT"
                url = (
                    f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}"
                )
                req = urllib.request.Request(url, headers={"User-Agent": "claw"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                    tickers = data.get("data", [])
                    if tickers:
                        price = float(tickers[0].get("last", 0))

            if price and price > 0:
                if anchor_price is None:
                    anchor_price = price
                else:
                    change_pct = (
                        (price - anchor_price) / anchor_price
                    ) * 100
                    if abs(change_pct) >= threshold_pct:
                        spike = PriceSpike(
                            symbol=symbol,
                            exchange=exchange,
                            old_price=anchor_price,
                            new_price=price,
                            change_pct=change_pct,
                        )
                        await spike_queue.put(spike)
                        logger.warning("Price spike (REST): %s", spike)
                        anchor_price = price

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("REST price poll error for %s@%s: %s", symbol, exchange, exc)

        await asyncio.sleep(interval)


async def watch_system_vitals(
    spike_queue: asyncio.Queue[Any],
    interval: float = 5.0,
    memory_threshold: float = 94.0,
    cpu_threshold: float = 95.0,
) -> None:
    """
    Poll CPU and memory every *interval* seconds.

    Fires a SystemSpike when memory > memory_threshold% or CPU > cpu_threshold%.
    Uses cross-platform methods; degrades gracefully if psutil is unavailable.
    """
    # Debounce: don't re-fire the same spike type within this window.
    _last_fired: dict[str, float] = {}
    debounce_sec = 60.0

    while True:
        try:
            mem_pct = _get_memory_pct()
            cpu_pct = _get_cpu_pct()

            now = time.time()

            if mem_pct is not None and mem_pct > memory_threshold:
                if now - _last_fired.get("memory", 0) > debounce_sec:
                    spike = SystemSpike(
                        metric="memory",
                        value=mem_pct,
                        threshold=memory_threshold,
                    )
                    await spike_queue.put(spike)
                    logger.warning("System spike: %s", spike)
                    _last_fired["memory"] = now

            if cpu_pct is not None and cpu_pct > cpu_threshold:
                if now - _last_fired.get("cpu", 0) > debounce_sec:
                    spike = SystemSpike(
                        metric="cpu",
                        value=cpu_pct,
                        threshold=cpu_threshold,
                    )
                    await spike_queue.put(spike)
                    logger.warning("System spike: %s", spike)
                    _last_fired["cpu"] = now

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("System vitals poll error: %s", exc)

        await asyncio.sleep(interval)


async def watch_file_changes(
    paths: list[str],
    spike_queue: asyncio.Queue[Any],
    interval: float = 2.0,
) -> None:
    """
    Poll file mtimes every *interval* seconds.

    Fires a FileSpike for any file whose mtime has changed since the last check.
    """
    mtimes: dict[str, float] = {}

    # Seed initial mtimes
    for p in paths:
        try:
            mtimes[p] = os.stat(p).st_mtime
        except OSError:
            mtimes[p] = 0.0

    while True:
        try:
            for p in paths:
                try:
                    new_mtime = os.stat(p).st_mtime
                except OSError:
                    # File doesn't exist yet — will fire when it appears
                    continue

                old_mtime = mtimes.get(p, 0.0)
                if new_mtime != old_mtime and old_mtime > 0:
                    spike = FileSpike(
                        file_path=p,
                        old_mtime=old_mtime,
                        new_mtime=new_mtime,
                    )
                    await spike_queue.put(spike)
                    logger.info("File spike: %s", spike)

                mtimes[p] = new_mtime

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("File watch poll error: %s", exc)

        await asyncio.sleep(interval)


async def watch_balance(
    workspace: Path,
    spike_queue: asyncio.Queue[Any],
    threshold_usd: float = 100.0,
    interval: float = 30.0,
) -> None:
    """
    Poll .claw/fund_estimate.json periodically.

    Fires a BalanceSpike when the reported USD balance drops below
    *threshold_usd*.  Debounces to avoid repeated spikes within 5 minutes.
    """
    fund_path = workspace / ".claw" / "fund_estimate.json"
    last_fired: float = 0.0
    debounce_sec = 300.0  # 5 minutes

    while True:
        try:
            if fund_path.is_file():
                data = json.loads(fund_path.read_text(encoding="utf-8"))
                current_usd = float(data.get("balance_usd", data.get("estimated_usd", 0.0)))
                now = time.time()
                if current_usd < threshold_usd and (now - last_fired) > debounce_sec:
                    spike = BalanceSpike(
                        current_usd=current_usd,
                        threshold_usd=threshold_usd,
                    )
                    await spike_queue.put(spike)
                    logger.warning("Balance spike: %s", spike)
                    last_fired = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Balance watch poll error: %s", exc)

        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# System metrics helpers
# ---------------------------------------------------------------------------

def _get_memory_pct() -> float | None:
    """Return memory usage percentage, or None if unavailable."""
    try:
        import psutil  # type: ignore[import-untyped]
        return psutil.virtual_memory().percent
    except ImportError:
        pass

    # Fallback: read /proc/meminfo on Linux
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        if total > 0:
            return ((total - available) / total) * 100
    except (OSError, ValueError):
        pass

    # Fallback: Windows via ctypes
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        return float(stat.dwMemoryLoad)
    except Exception:
        pass

    return None


def _get_cpu_pct() -> float | None:
    """Return CPU usage percentage, or None if unavailable."""
    try:
        import psutil  # type: ignore[import-untyped]
        return psutil.cpu_percent(interval=0.5)
    except ImportError:
        pass

    # Rough fallback on Linux: parse /proc/stat
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            line1 = f.readline()
        fields = [int(x) for x in line1.split()[1:]]
        idle1 = fields[3]
        total1 = sum(fields)
        time.sleep(0.5)
        with open("/proc/stat", encoding="utf-8") as f:
            line2 = f.readline()
        fields2 = [int(x) for x in line2.split()[1:]]
        idle2 = fields2[3]
        total2 = sum(fields2)
        diff_idle = idle2 - idle1
        diff_total = total2 - total1
        if diff_total > 0:
            return (1.0 - diff_idle / diff_total) * 100
    except (OSError, ValueError, IndexError):
        pass

    return None


# ---------------------------------------------------------------------------
# SpikeDetector — the neuron registry
# ---------------------------------------------------------------------------

class SpikeDetector:
    """
    Registry of spike neurons. Manages watches and runs the async event loop.

    Usage::

        detector = SpikeDetector(workspace, on_spike=my_callback)
        detector.add_price_watch("BTCUSDT", "binance", 5.0)
        detector.add_file_watch("/path/to/file")
        detector.add_memory_watch(94)
        detector.start()   # runs in background thread
        ...
        detector.stop()
    """

    def __init__(
        self,
        workspace: str | Path,
        on_spike: Callable[[Any], None],
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.on_spike = on_spike
        self._price_watches: list[PriceWatch] = []
        self._file_watches: list[FileWatch] = []
        self._balance_watches: list[BalanceWatch] = []
        self._memory_watches: list[MemoryWatch] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def add_price_watch(
        self,
        symbol: str,
        exchange: str,
        threshold_pct: float = 5.0,
    ) -> None:
        """Watch for price moves exceeding threshold_pct on an exchange."""
        self._price_watches.append(
            PriceWatch(symbol=symbol, exchange=exchange, threshold_pct=threshold_pct)
        )

    def add_file_watch(self, file_path: str | Path) -> None:
        """Watch for file modifications (mtime changes)."""
        self._file_watches.append(FileWatch(file_path=str(file_path)))

    def add_balance_watch(self, threshold_usd: float) -> None:
        """Watch for balance drops below threshold (USD)."""
        self._balance_watches.append(BalanceWatch(threshold_usd=threshold_usd))

    def add_memory_watch(self, threshold_pct: float = 94.0) -> None:
        """Watch for memory usage exceeding threshold percent."""
        self._memory_watches.append(MemoryWatch(threshold_pct=threshold_pct))

    def start(self) -> None:
        """Start the spinal cord in a background daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="claw-spinal-cord",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Spinal cord started with %d price, %d file, %d balance, %d memory watches",
            len(self._price_watches),
            len(self._file_watches),
            len(self._balance_watches),
            len(self._memory_watches),
        )

    def stop(self) -> None:
        """Signal the spinal cord to stop."""
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("Spinal cord stopped")

    def _run_loop(self) -> None:
        """Internal: run the asyncio event loop in the background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as exc:
            logger.error("Spinal cord loop crashed: %s", exc)
        finally:
            self._loop.close()

    async def _main(self) -> None:
        """Orchestrate all watchers and the spike dispatcher."""
        spike_queue: asyncio.Queue[Any] = asyncio.Queue()
        tasks: list[asyncio.Task[None]] = []

        # Spike dispatcher: reads queue and calls on_spike
        tasks.append(asyncio.ensure_future(
            self._dispatch_spikes(spike_queue)
        ))

        # Price watchers
        for pw in self._price_watches:
            tasks.append(asyncio.ensure_future(
                watch_price_websocket(
                    pw.symbol, pw.exchange, spike_queue, pw.threshold_pct
                )
            ))

        # File watchers (single coroutine for all paths)
        file_paths = [fw.file_path for fw in self._file_watches]
        if file_paths:
            tasks.append(asyncio.ensure_future(
                watch_file_changes(file_paths, spike_queue, interval=2.0)
            ))

        # System vitals (memory + CPU)
        mem_threshold = 94.0
        if self._memory_watches:
            mem_threshold = min(mw.threshold_pct for mw in self._memory_watches)
        tasks.append(asyncio.ensure_future(
            watch_system_vitals(
                spike_queue,
                interval=5.0,
                memory_threshold=mem_threshold,
            )
        ))

        # Balance watchers
        for bw in self._balance_watches:
            tasks.append(asyncio.ensure_future(
                watch_balance(
                    workspace=self.workspace,
                    spike_queue=spike_queue,
                    threshold_usd=bw.threshold_usd,
                    interval=30.0,
                )
            ))

        # Wait until stopped
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _dispatch_spikes(self, spike_queue: asyncio.Queue[Any]) -> None:
        """Read spikes from the queue and invoke the callback."""
        while True:
            try:
                spike = await asyncio.wait_for(spike_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

            try:
                self.on_spike(spike)
            except Exception as exc:
                logger.error("on_spike callback error: %s", exc)


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def create_default_watches(workspace: str | Path) -> list[dict[str, Any]]:
    """
    Create a sensible set of default watches for a DevClaw workspace.

    Returns a list of watch descriptors (for serialization/logging).
    """
    ws = Path(workspace).resolve()
    watches: list[dict[str, Any]] = [
        {"type": "price", "symbol": "BTCUSDT", "exchange": "binance", "threshold_pct": 5.0},
        {"type": "price", "symbol": "ETHUSDT", "exchange": "binance", "threshold_pct": 5.0},
        {"type": "memory", "threshold_pct": 94.0},
        {"type": "file", "path": str(ws / ".claw" / "survival_state.json")},
        {"type": "file", "path": str(ws / ".claw" / "operator_inbox.jsonl")},
    ]
    return watches


def run_spinal_cord(
    workspace: str | Path,
    on_spike: Callable[[Any], None],
    watches: list[dict[str, Any]] | None = None,
) -> SpikeDetector:
    """
    Start the async event loop with all configured watchers in a background thread.

    When a spike fires, *on_spike* is called (which can wake the main brain).

    Args:
        workspace: DevClaw workspace root path.
        on_spike: Callback invoked with each spike event.
        watches: Optional list of watch descriptors. Uses create_default_watches()
                 if not provided.

    Returns:
        The running SpikeDetector instance (call .stop() to shut down).
    """
    ws = Path(workspace).resolve()
    if watches is None:
        watches = create_default_watches(ws)

    detector = SpikeDetector(ws, on_spike=on_spike)

    for w in watches:
        wtype = w.get("type", "")
        if wtype == "price":
            detector.add_price_watch(
                symbol=w.get("symbol", "BTCUSDT"),
                exchange=w.get("exchange", "binance"),
                threshold_pct=w.get("threshold_pct", 5.0),
            )
        elif wtype == "file":
            path = w.get("path", "")
            if path:
                detector.add_file_watch(path)
        elif wtype == "balance":
            detector.add_balance_watch(
                threshold_usd=w.get("threshold_usd", 100.0),
            )
        elif wtype == "memory":
            detector.add_memory_watch(
                threshold_pct=w.get("threshold_pct", 94.0),
            )
        else:
            logger.warning("Unknown watch type: %r", wtype)

    detector.start()
    return detector

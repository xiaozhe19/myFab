"""SQLite 的 ResultStorage 实现。多线程版本"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

import sqlite3
from pathlib import Path
import json

from fab.model.entities import FabModel
from fab.plugins.base import FabPlugin, PluginResultContext
from fab.plugins.storage import LotEventRecord, OperationRecord, ResultStorage


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY,
    strategy TEXT
);
CREATE TABLE IF NOT EXISTS operations (
    run_id INTEGER NOT NULL,
    lot_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    process TEXT NOT NULL,
    step INTEGER NOT NULL,
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    duration REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lot_events (
    event_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    lot_id TEXT NOT NULL,
    event_time REAL NOT NULL,
    event_type TEXT NOT NULL,
    step INTEGER,
    process TEXT,
    tool_id TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    run_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plugin_records (
    record_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    plugin_name TEXT NOT NULL,
    record_type TEXT NOT NULL,
    event_time REAL,
    payload_json TEXT NOT NULL
);
"""

BATCH_SIZE = 5_000 #积累n条记录后一起executemany
QUEUE_SIZE = 20_000 #主线程最多领先数据库n条记录，queue满了主线程阻塞

@dataclass(frozen=True)
class _MetricsMessage:
    measurement: dict[str, float | int]


@dataclass(frozen=True)
class _ExtensionMessage:
    plugin_name: str
    record_type: str
    payload: dict[str, object]
    event_time: float | None


@dataclass(frozen=True)
class _FinishMessage:
    strategy_name: str

class SQLiteStoragePlugin(FabPlugin, ResultStorage):
    """仅负责 SQLite 连接、SQL 执行和事务管理。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self._queue: queue.Queue[object]|None = None
        self._worker: threading.Thread|None = None
        self._ready = threading.Event()
        self._finished = threading.Event()
        self._worker_error:BaseException|None = None

    def provided_services(self) -> dict[str, object]:
        return {"result_storage": self}

    def on_simulation_started(self, model: FabModel) -> None:
        self.start_run()

    def on_simulation_finished(self, context: PluginResultContext) -> None:
        self.finish_run(context.strategy_name)

    def start_run(self) -> None:
        if self._worker is not None:
            raise RuntimeError("SQLiteStoragePlugin 已经启动。")

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._ready.clear()
        self._finished.clear()
        self._worker_error = None

        self._worker = threading.Thread(
            target=self._run_worker,
            name="sqlite-storage",
            daemon=False,
        )

        self._worker.start()
        self._ready.wait()
        self._raise_worker_error()

    def _raise_worker_error(self) -> None:
        if self._worker_error is not None:
            raise RuntimeError("SQLite 写入线程失败。") from self._worker_error

    def _enqueue(
            self,
            message: object,
    ) -> None:
        if self._queue is None:
            raise RuntimeError("SQLiteStoragePlugin 尚未启动。")

        while True:
            self._raise_worker_error()
            try:
                self._queue.put(message, timeout=0.1)
                return
            except queue.Full:
                continue
    
    def append_operation(self, record: OperationRecord) -> None:
        self._enqueue(record)

    def append_lot_event(self, record: LotEventRecord) -> None:
        self._enqueue(record)

    def append_metrics(self, measurement: dict[str, float | int]) -> None:
        self._enqueue(_MetricsMessage(measurement))

    def append_extension_data(
        self,
        plugin_name: str,
        record_type: str,
        payload: dict[str, object],
        event_time: float | None = None,
    ) -> None:
        self._enqueue(_ExtensionMessage(
            plugin_name,
            record_type, 
            payload, 
            event_time,
            ))
            
           
    def finish_run(self, strategy_name: str) -> None:
        self._enqueue(_FinishMessage(strategy_name))

        if self._worker is None:
            raise RuntimeError("SQLiteStoragePlugin 尚未启动。")

        self._worker.join()
        self._raise_worker_error()

    def _run_worker(self) -> None:
        connection: sqlite3.Connection | None = None

        try:
            connection = sqlite3.connect(self.database_path)
            connection.executescript(SCHEMA)
            cursor = connection.execute(
                "INSERT INTO runs (strategy) VALUES (?)",
                (None,),
            )
            run_id = cursor.lastrowid
            if run_id is None:
                raise RuntimeError("无法获取 run_id。")
            connection.commit()
            self._ready.set()

            operations: list[OperationRecord] = []
            lot_events: list[LotEventRecord] = []
            metrics: list[tuple[str, float | int]] = []
            extensions: list[_ExtensionMessage] = []

            def flush() -> None:
                if operations:
                    connection.executemany(
                        """
                        INSERT INTO operations (
                            run_id, lot_id, product_id, tool_id, process,
                            step, start_time, end_time, duration
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                run_id,
                                item.lot_id,
                                item.product_id,
                                item.tool_id,
                                item.process,
                                item.step,
                                item.start_time,
                                item.end_time,
                                item.duration,
                            )
                            for item in operations
                        ],
                    )
                    operations.clear()

                if lot_events:
                    connection.executemany(
                        """
                        INSERT INTO lot_events (
                            run_id, lot_id, event_time, event_type,
                            step, process, tool_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                run_id,
                                item.lot_id,
                                item.event_time,
                                item.event_type,
                                item.step,
                                item.process,
                                item.tool_id,
                            )
                            for item in lot_events
                        ],
                    )
                    lot_events.clear()

                if metrics:
                    connection.executemany(
                        "INSERT INTO metrics (run_id, name, value) VALUES (?, ?, ?)",
                        [(run_id, name, value) for name, value in metrics],
                    )
                    metrics.clear()

                if extensions:
                    connection.executemany(
                        """
                        INSERT INTO plugin_records (
                            run_id, plugin_name, record_type,
                            event_time, payload_json
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                run_id,
                                item.plugin_name,
                                item.record_type,
                                item.event_time,
                                json.dumps(
                                    item.payload,
                                    ensure_ascii=False,
                                    default=str,
                                ),
                            )
                            for item in extensions
                        ],
                    )
                    extensions.clear()

                connection.commit()

            while True:
                if self._queue is None:
                    raise RuntimeError("SQLiteStoragePlugin 尚未初始化队列。")
                message = self._queue.get()

                if isinstance(message, OperationRecord):
                    operations.append(message)
                elif isinstance(message, LotEventRecord):
                    lot_events.append(message)
                elif isinstance(message, _MetricsMessage):
                    metrics.extend(message.measurement.items())
                elif isinstance(message, _ExtensionMessage):
                    extensions.append(message)
                elif isinstance(message, _FinishMessage):
                    flush()
                    connection.execute(
                        "UPDATE runs SET strategy = ? WHERE run_id = ?",
                        (message.strategy_name, run_id),
                    )
                    connection.commit()
                    return
                else:
                    raise TypeError(f"未知的 SQLite 写入消息：{type(message).__name__}")

                if (
                    len(operations)
                    + len(lot_events)
                    + len(metrics)
                    + len(extensions)
                    >= BATCH_SIZE
                ):
                    flush()

        except BaseException as e:
            self._worker_error = e
            self._ready.set()

        finally:
            if connection is not None:
                connection.close()
            self._finished.set()

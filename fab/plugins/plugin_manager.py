#插件管理器
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Sequence

from fab.model.entities import FabModel, LotState, ToolState
from fab.plugins.base import FabPlugin, PluginResultContext
from fab.strategy import StrategyDecision

class PluginManager:

    def __init__(self, plugins:Iterable[FabPlugin] =()) -> None:
        self.plugins = tuple(plugins)
        self._services = self._collect_services()
        self._bind_services()
        self._simulation_started_callbacks = self._callbacks(
            "on_simulation_started"
        )
        self._simulation_finished_callbacks = self._callbacks(
            "on_simulation_finished"
        )
        self._time_advanced_callbacks = self._callbacks(
            "on_time_advanced"
        )
        self._lot_released_callbacks = self._callbacks(
            "on_lot_released"
        )
        self._operation_completed_callbacks = self._callbacks(
            "on_operation_completed"
        )
        self._lot_completed_callbacks = self._callbacks(
            "on_lot_completed"
        )
        self._decision_made_callbacks = self._callbacks(
            "on_decision_made"
        )
        self._cqt_violation_callbacks = self._callbacks(
            "on_cqt_violation"
        )
        self._result_callbacks = self._callbacks(
            "build_result"
        )


        self.has_time_advanced_plugins = bool(
            self._time_advanced_callbacks
        )

        self.has_operation_plugins = bool(
            self._operation_completed_callbacks
        )

        self.has_decision_plugins = bool(
            self._decision_made_callbacks
        )

        self.has_cqt_plugins = bool(
            self._cqt_violation_callbacks
        )

    def _collect_services(self) -> dict[str, object]:
        services: dict[str, object] = {}

        for plugin in self.plugins:
            for name, service in plugin.provided_services().items():
                if name in services:
                    raise ValueError(
                        f"多个插件提供了相同的服务：{name}。"
                    )
                services[name] = service
        return services

    def _bind_services(self) -> None:
        for plugin in self.plugins:
            missing = [
                name
                for name in plugin.required_services()
                if name not in self._services
            ]
            if missing:
                names = ",".join(missing)
                raise ValueError(
                    f"插件 {type(plugin).__name__} 需要未提供的服务：{names}。"
                )
            plugin.bind_services(self._services)

    def _callbacks(
            self,
            method_name:str
    ) -> tuple[Callable[...,object],...]:
        base_method = getattr(FabPlugin,method_name)
        return tuple(
            getattr(plugin, method_name)
            for plugin in self.plugins
            if getattr(type(plugin),method_name) is not base_method
        )

    def on_simulation_started(
            self,
            model: FabModel,
    ) -> None:
        for callback in self._simulation_started_callbacks:
            callback(model)
    def on_time_advanced(
        self,
        current_time: float,
        lots: Sequence[LotState],
    ) -> None:
        for callback in self._time_advanced_callbacks:
            callback(current_time, lots)

    def on_lot_released(
        self,
        current_time: float,
        lot: LotState,
    ) -> None:
        for callback in self._lot_released_callbacks:
            callback(current_time, lot)

    def on_operation_completed(
        self,
        lot: LotState,
        tool: ToolState,
        start: float,
        end: float,
        active_duration: float,
    ) -> None:
        for callback in self._operation_completed_callbacks:
            callback(
                lot,
                tool,
                start,
                end,
                active_duration,
            )

    def on_lot_completed(
        self,
        current_time: float,
        lot: LotState,
    ) -> None:
        for callback in self._lot_completed_callbacks:
            callback(current_time, lot)

    def on_decision_made(
        self,
        current_time: float,
        decision_kind: str,
        decision: StrategyDecision,
    ) -> None:
        for callback in self._decision_made_callbacks:
            callback(
                current_time,
                decision_kind,
                decision,
            )

    def on_cqt_violation(
        self,
        current_time: float,
        lot_id: str,
        target_step: int,
    ) -> None:
        for callback in self._cqt_violation_callbacks:
            callback(
                current_time,
                lot_id,
                target_step,
            )

    def build_result(
        self,
        context: PluginResultContext,
    ) -> dict[str, object]:
        result: dict[str, object] = {}

        for callback in self._result_callbacks:
            plugin_result = callback(context)

            duplicated_keys = result.keys() & plugin_result.keys()
            if duplicated_keys:
                duplicated = ", ".join(sorted(duplicated_keys))
                raise ValueError(
                    f"多个插件生成了相同的结果字段：{duplicated}。"
                )

            result.update(plugin_result)

        return result

    def on_simulation_finished(
        self,
        context: PluginResultContext
    ) -> None:
        for callback in self._simulation_finished_callbacks:
            callback(context)

"""Чтение кластера для дашборда: Argo, MLSC, nginx, submitter-под, узлы пула. Ничего не пишет."""

from fleet_console.observe.observer import ClusterObserver, Source

__all__ = ["ClusterObserver", "Source"]

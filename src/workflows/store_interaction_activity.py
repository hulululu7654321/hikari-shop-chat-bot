# -*- coding: utf-8 -*-
"""
store_interaction_activity.py — 店铺交互适配层
=====================================================================
职责：作为 core 层与 services.store_service 的桥接入口。

调用链：
  core（dialogue_engine / orchestrator）
    → workflows/store_interaction_activity（re-export）
    → services/store_service（实现）

符合 MASTER v3.0 单向依赖规范：
  core 层不直接 import services 层，通过 workflows 层中转。
  workflows 层可以 import services 层。
=====================================================================
"""

from src.services.store_service import (
    push_live2d_motion,
    push_name_learned,
    get_store_packages_prompt as get_store_packages_hint,
)

__all__ = [
    "push_live2d_motion",
    "push_name_learned",
    "get_store_packages_hint",
]

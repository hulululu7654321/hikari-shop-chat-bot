# -*- coding: utf-8 -*-
from src.modules.manage.api import bp, init_manage_api
from src.modules.manage.admin_api import init_admin_api

__all__ = ["bp", "init_manage_api", "init_admin_api"]

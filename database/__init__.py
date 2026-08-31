"""CareerCopilot 数据库访问层。"""

from database.db_handler import get_connection, init_db

__all__ = ["get_connection", "init_db"]

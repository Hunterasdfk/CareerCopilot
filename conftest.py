"""pytest 根配置。

空文件；其作用是让 pytest（prepend 导入模式）把项目根目录加入 sys.path，
保证测试模块可以直接 `import database...` / `import services...`。
"""

from __future__ import annotations

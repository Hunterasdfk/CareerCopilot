"""CareerCopilot 主应用入口（Home 页）。

本文件作为 Streamlit Home 页，提供应用导航。
业务页面 import_page.py / dashboard.py 已在 pages/ 目录实现。
"""

from __future__ import annotations

import streamlit as st

from config.settings import APP_SUBHEADER, APP_TITLE


def main() -> None:
    """渲染 Home 页。"""

    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="🎯",
        layout="wide",
    )

    st.title(APP_TITLE)
    st.subheader(APP_SUBHEADER)

    st.markdown(
        """
        ---
        **MVP 0.1 已完成** —— 可通过侧边栏使用机会导入与机会看板。

        已上线页面：

        | 页面 | 说明 | 状态 |
        | :--- | :--- | :--- |
        | 机会导入 | 上传工作簿、布局识别、字段映射、导入预览 | 已完成 |
        | 机会看板 | 浏览、筛选、状态管理、候选清单 | 已完成 |

        请通过侧边栏访问各页面。
        """
    )


if __name__ == "__main__":
    main()

"""三国杀线下助手资料库核心包。

设计原则（详见 README.md / docs/design.md）：

- ``data/`` 下的 YAML 是**唯一权威数据源**，Git 管理。
- ``build/sgs.db`` 是 SQLite 生成物，随时可由 YAML 完整重建，不手工修改。
- 本包提供：schema（数据模型）/ loader（YAML 装载）/ validate（校验）/
  db（建库）/ repo（查询）/ render（文本渲染）/ cli（命令行）。

后续接微信小程序时，只需在 ``repo.py`` 之上加一层 FastAPI，无需改动数据层。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]

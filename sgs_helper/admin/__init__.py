"""本地资料管理网页（FastAPI + Jinja2）。

仅用于本机维护 ``data/`` 下的 YAML 权威源；SQLite 生成物永远由
``sgs_helper.db.build_database`` 全量重建，网页不会直接改数据库。

启动::

    uvicorn sgs_helper.admin.app:app --host 127.0.0.1 --port 8000

模块划分：

- ``app.py``          FastAPI 路由与页面装配
- ``store.py``        读 YAML（快照缓存 + 内存检索）、写 YAML、重建 SQLite
- ``forms.py``        由 Pydantic 模型半自动生成表单，并把提交值转回实体
- ``yaml_writer.py``  可读 YAML 的读写（保留注释头、块状标量、原子落盘）

本文件刻意不 import 任何子模块，这样 ``sgs_helper.admin.yaml_writer`` 之类的工具
在没安装 FastAPI 的环境里也能直接使用。
"""

__all__ = ["app", "store", "forms", "yaml_writer"]

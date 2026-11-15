"""领域层：不知道"聊天"是什么的业务能力。

按作用分组（每个子目录一个领域）：

- ``dialogue``  对话理解与生成口径（情绪、人设、发言规则、意图）
- ``memory``    记忆与检索（长期记忆、RAG、对话蒸馏）
- ``catalog``   内容管理（书、角色、合并、抽取）
- ``ops``       运维杂项（日志、清理、媒体、housekeeping）
- ``runtime``   小型工具提供者（本地时间、问候判断）

依赖方向：``core → services → skills → agent → api``。
本层不能 import ``agent`` 或 ``skills``（有 AST 扫描器守着）。
"""

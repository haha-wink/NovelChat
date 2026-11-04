"""集中式配置：路径 / 密钥 / 抽取与聊天参数。

全部来自环境变量或 ``backend/.env``；路径基于 ``__file__`` 解析，
不依赖进程的当前工作目录。
"""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# backend/ 目录（本文件位于 backend/core/config.py）
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent

#: 进程内随机会话密钥。仅在未配置 SESSION_SECRET 时使用，
#: 重启后旧 token 全部失效（开发期可接受）。
_EPHEMERAL_SESSION_SECRET = secrets.token_urlsafe(48)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 模型 ----------
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    #: 角色抽取用的模型，留空则复用 deepseek_model
    extraction_model: str = ""
    #: 兼容旧前端：允许请求体自带 API Key（默认关闭，密钥只留在服务端）
    allow_client_api_key: bool = False

    # ---------- 服务 ----------
    host: str = "127.0.0.1"
    port: int = 8000
    #: 逗号分隔；"*" 表示允许全部来源（此时自动关闭 credentials）
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    session_secret: str = ""
    session_ttl_seconds: int = 7 * 24 * 3600

    # ---------- 存储 ----------
    #: 留空则使用 backend/data；相对路径按 backend/ 解析
    data_dir: str = ""

    # ---------- 上传与解析 ----------
    max_upload_bytes: int = 20 * 1024 * 1024
    #: 单本小说正文上限（字符数）
    max_book_chars: int = 2_000_000
    #: 正文下限，太短抽不出角色
    min_book_chars: int = 200

    # ---------- 角色抽取 ----------
    chunk_chars: int = 6000
    chunk_overlap: int = 400
    #: 最多分多少块（决定 LLM 调用次数与成本）
    max_chunks: int = 40
    #: 每块最多抽几个角色
    characters_per_chunk: int = 6
    #: 每本书最多保留几个角色
    max_characters: int = 20

    # ---------- 抽取方式 ----------
    #: algo   = 纯算法，零 API 成本（只有可量化特征）
    #: hybrid = 算法出骨架 + LLM 分批补全性格/背景（性价比最高）
    #: agent  = 算法 + 角色档案管理员 Agent（审核 / 联网校验 / 调优 / 抓头像）+ 补全
    #: llm    = 逐块全文抽取（质量上限最高、最贵）
    extraction_mode: str = "hybrid"
    #: 算法模式下，一个名字至少出现几次才算角色
    miner_min_mentions: int = 3
    #: hybrid 模式下每个角色给模型参考的台词条数 / 旁白片段数
    hybrid_quote_samples: int = 6
    hybrid_narrative_samples: int = 2
    #: hybrid 模式额外附带的原文参考片段总量（字符），用于校验与补漏
    hybrid_reference_chars: int = 2000
    #: hybrid 模式每批补全几个角色（模型输出上限决定；批次太多也没关系，只是多几次调用）
    hybrid_batch_size: int = 10

    # ---------- 角色档案管理员 Agent ----------
    #: agent 模式最多走几步（每步一次 LLM 调用 + 一个工具）
    agent_max_steps: int = 8
    #: 允许 agent 联网检索书名对应的角色表（需要本机能出网）
    agent_enable_web: bool = True
    #: 允许 agent 爬取角色头像候选
    agent_enable_avatars: bool = True
    #: 单个角色最多保留几个头像候选
    avatar_max_candidates: int = 6

    # ---------- 人名识别引擎 ----------
    #: auto   = 先试 jieba，再试 HanLP，都没有就用内置规则
    #: jieba  = 只用 jieba（没装则报错提示，不静默降级）
    #: hanlp  = 只用 HanLP
    #: builtin= 只用内置规则（不依赖第三方库）
    name_engine: str = "auto"

    # ---------- 角色初始记忆（原文片段） ----------
    excerpt_chunks: int = 3
    excerpt_chars: int = 1200

    # ---------- Agent / 记忆 ----------
    agent_cache_size: int = 8
    history_runs: int = 8

    # ---------- 聊天 ----------
    max_prompt_chars: int = 8000
    max_images: int = 4
    max_image_bytes: int = 8 * 1024 * 1024
    max_total_image_bytes: int = 20 * 1024 * 1024

    # ---------- 群聊（一本书架 = 一个群）----------
    #: 一轮最多几个人发言（成本红线：乘上 max_rounds 就是单回合最多几次模型调用）
    group_max_speakers: int = 2
    #: 一个回合最多几轮（角色 @ 别人会触发下一轮）
    group_max_rounds: int = 2
    #: 发言资格阈值：低于它就不说话（宁可不热闹，也别谁都插一句）
    group_speak_threshold: float = 0.55
    #: 被 @全体成员 时阈值下调到这里
    group_all_threshold: float = 0.35
    #: 同一角色两次发言的最小间隔（秒），防止话痨垄断
    group_cooldown_seconds: int = 45

    # ---------- RAG 记忆检索 ----------
    #: auto = 模型在就用向量，lexical = 强制纯词法（省内存/排障用）
    rag_backend: str = "auto"
    #: 留空则用 backend/data/models/bge-small-zh-v1.5
    rag_model_path: str = ""
    #: 单次检索最多带几条记忆进提示词
    rag_top_k: int = 4
    #: 编码截断长度。原文片段普遍 800~1200 字，128 token 只能看到开头；
    #: 256 是质量与延迟的折中（实测单次查询 20~25ms）
    rag_max_length: int = 256
    #: torch 线程数；0 = 用 torch 默认
    rag_threads: int = 0

    # ---------- 对话蒸馏（短期 -> 长期记忆）----------
    #: 未蒸馏的对话条数超过它就自动蒸馏；0 = 关闭自动蒸馏
    chat_distill_threshold: int = 40
    #: 蒸馏后保留最近几天的对话原文
    chat_retention_days: int = 7

    # ---------- 情绪小模型（ONNX）----------
    #: auto = 模型在就用 ONNX，否则规则；onnx = 强制；rules = 只用词表
    emotion_model: str = "auto"
    #: ONNX 情绪模型目录；留空用 backend/data/models/erlangshen-sentiment
    emotion_onnx_dir: str = ""

    # ------------------------------------------------------------------
    # 派生值
    # ------------------------------------------------------------------
    def _resolve(self, raw: str, default: Path) -> Path:
        if not raw:
            return default
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (BACKEND_DIR / path)

    @property
    def data_path(self) -> Path:
        return self._resolve(self.data_dir, BACKEND_DIR / "data")

    @property
    def workspace_path(self) -> Path:
        return self.data_path / "workspace"

    @property
    def library_path(self) -> Path:
        return self.workspace_path / "books"

    @property
    def uploads_path(self) -> Path:
        return self.workspace_path / "uploads"

    @property
    def db_path(self) -> Path:
        return self.workspace_path / "app.db"

    @property
    def memory_db_path(self) -> Path:
        """Agno 的对话记忆库。"""
        return self.workspace_path / "agent_memory.db"

    @property
    def extraction_model_name(self) -> str:
        return (self.extraction_model or self.deepseek_model).strip()

    @property
    def extraction_mode_name(self) -> str:
        mode = (self.extraction_mode or "").strip().lower()
        return mode if mode in ("algo", "hybrid", "llm", "agent") else "hybrid"

    @property
    def origins(self) -> list[str]:
        items = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return items or ["http://localhost:5173"]

    @property
    def allow_credentials(self) -> bool:
        # 浏览器规范禁止 `Access-Control-Allow-Origin: *` 与 credentials 同时生效
        return "*" not in self.origins

    @property
    def session_secret_bytes(self) -> bytes:
        if self.session_secret:
            return self.session_secret.encode("utf-8")
        if not getattr(self, "_warned_ephemeral", False):
            logger.warning(
                "未配置 SESSION_SECRET，正在使用进程内随机密钥；"
                "服务重启后所有会话 token 失效。生产环境请在 backend/.env 中设置。"
            )
            object.__setattr__(self, "_warned_ephemeral", True)
        return _EPHEMERAL_SESSION_SECRET.encode("utf-8")

    def api_key_for(self, client_key: str | None = None) -> str | None:
        """本次请求用哪个 DeepSeek Key。默认只用服务端配置。"""
        if self.allow_client_api_key and client_key and client_key.strip():
            return client_key.strip()
        key = self.deepseek_api_key.strip()
        return key or None

    def ensure_dirs(self) -> None:
        for path in (self.data_path, self.workspace_path, self.library_path, self.uploads_path):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

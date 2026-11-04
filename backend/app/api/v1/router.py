"""把各域路由汇总成一个 APIRouter（main.py 只挂这一个）。"""

from fastapi import APIRouter

from api.v1.endpoints import books, characters, chat, knowledge, shelves, system

router = APIRouter()

for _module in (system, books, shelves, characters, knowledge, chat):
    router.include_router(_module.router)

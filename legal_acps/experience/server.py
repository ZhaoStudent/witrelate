"""网络诽谤演练的 Web 服务（FastAPI）—— 当事人替自己辩白，对局后由裁判揭示。

启动（在 witrelate 目录下）：

    # 真模型（读 .env 里的 OPENAI_MODEL / OPENAI_API_KEY）
    ./.venv/bin/python -m legal_acps.experience.server --port 8090

    # 假模型：不需要任何 key，只用来验证接线是否通畅
    ./.venv/bin/python -m legal_acps.experience.server --stub --port 8090

然后浏览器打开 http://127.0.0.1:8090

⚠️ 服务默认只听 127.0.0.1。要在服务器上跑、本地浏览器看，用 SSH 端口转发：

    ssh -L 8090:127.0.0.1:8090 -i <key> ubuntu@<server>

   —— 不要为了图省事把 --host 改成 0.0.0.0（那会把一个"教人识别话术"的
   未鉴权服务直接暴露到公网）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from dotenv_lite import load_env

from .session import (
    DEFAULT_DIFFICULTY,
    DIFFICULTY_ROUNDS,
    ExperienceSession,
    RevealRequired,
)

WEB_DIR = Path(__file__).resolve().parent / "web"

#: 内存会话表。单机演示足够；重启即清空（没有需要落盘的状态）。
SESSIONS: dict[str, ExperienceSession] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


class StartReq(BaseModel):
    event: str = Field(min_length=1, max_length=2000, description="你看到的那句话")
    difficulty: str = Field(
        default=DEFAULT_DIFFICULTY, description="难度：轻松 / 标准 / 困难"
    )

    @field_validator("difficulty")
    @classmethod
    def _known_difficulty(cls, value: str) -> str:
        """难度的**唯一真源**是 `DIFFICULTY_ROUNDS` —— 在这里派生出可选项。

        ⚠️ 刻意**不写死成 Literal["轻松","标准","困难"]**：那等于把难度清单
        抄了第二份，加/改一档时必漏一处（本项目在 `PORTS` / `serve.sh` /
        `systemd-install.sh` 上踩过同样的坑：同一事实散在四处，加一个角色必漏）。
        """
        if value not in DIFFICULTY_ROUNDS:
            raise ValueError(
                f"未知难度「{value}」，可选：{' / '.join(DIFFICULTY_ROUNDS)}"
            )
        return value


class ReplyReq(BaseModel):
    text: str = Field(min_length=1, max_length=2000, description="你（当事人）的回应")


class DetailsReq(BaseModel):
    """对受理官澄清问题的回答。`text` 留空 = 跳过（追问是可选的）。"""

    text: str = Field(
        default="",
        max_length=2000,
        description="对受理官问题的补充回答；留空表示跳过",
    )


class RevealReq(BaseModel):
    """揭示时一并提交的**当事人自己的判断**。

    刻意做成**可选 body**（`reveal` 端点的参数默认 None）：
    揭示是红线动作，不能因为客户端没带这个字段就拒绝服务 ——
    少一份对照是少一点教学价值，而拒绝揭示是**功能不可用**，两者不对等。
    """

    suspected: list[int] = Field(
        default_factory=list,
        max_length=200,
        description="你在局中标记为「可疑」的轮次号",
    )


def _lock_for(sid: str) -> asyncio.Lock:
    return _LOCKS.setdefault(sid, asyncio.Lock())


def _get(sid: str) -> ExperienceSession:
    session = SESSIONS.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新开局")
    return session


def create_app(*, use_stub: bool = False, verbose: bool = False) -> FastAPI:
    app = FastAPI(
        title="网络诽谤演练 · 你替自己辩白",
        description="让当事人亲身体验被话术编排的过程，并在最后被逐条揭示、"
        "连同他站住的反驳一起点评。",
        version="1.0.0",
    )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.post("/api/session")
    async def create_session(req: StartReq) -> dict[str, Any]:
        session = ExperienceSession(
            use_stub=use_stub, verbose=verbose, difficulty=req.difficulty
        )
        sid = uuid.uuid4().hex[:12]
        async with _lock_for(sid):
            try:
                state = await session.start(req.event)
            except ValueError as exc:
                # ⚠️ 纯空白输入（"   "）能通过 StartReq 的 min_length=1（长度是 3），
                #    随后在 session.start() 里因 strip() 后为空而抛 ValueError。
                #    早期这里只捕获 RuntimeError，未处理的 ValueError 直接变成
                #    **HTTP 500** —— 客户端输入不合法却报服务端错误，语义错得离谱。
                #    开局失败还要顺手清掉那把孤儿锁，否则 _LOCKS 会随失败次数增长。
                _LOCKS.pop(sid, None)
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:  # 受理失败：把排查提示原样给用户
                _LOCKS.pop(sid, None)
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        SESSIONS[sid] = session
        return {"session_id": sid, **state}

    @app.post("/api/session/{sid}/reply")
    async def reply(sid: str, req: ReplyReq) -> dict[str, Any]:
        session = _get(sid)
        async with _lock_for(sid):
            try:
                return await session.reply(req.text)
            except ValueError as exc:
                # 同 create_session：空白文本是客户端错误，不是 500
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/session/{sid}/details")
    async def details(sid: str, req: DetailsReq) -> dict[str, Any]:
        """回答（或跳过）受理官的澄清问题，然后才开辩。

        追问是**可选的**：text 留空 = 跳过，用现有受理结论直接开辩；
        不在 awaiting_details 相位调用这里 = 409（把状态机走错当客户端错误）。
        """
        session = _get(sid)
        async with _lock_for(sid):
            try:
                if (req.text or "").strip():
                    return await session.give_details(req.text)
                return await session.skip_details()
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/session/{sid}/reveal")
    async def reveal(sid: str, req: RevealReq | None = None) -> dict[str, Any]:
        session = _get(sid)
        async with _lock_for(sid):
            try:
                payload = await session.reveal(
                    suspected=(req.suspected if req is not None else None)
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            # `to_dict()` 本身就带 "reveal" 键，直接展开即可；
            # 早期写成 {"reveal": payload, **to_dict()}，前面的键会被后面的
            # 同名键**覆盖** —— 看着像在传 payload，其实是一句死表达式。
            return {**session.state.to_dict(), "reveal": payload}

    @app.get("/api/session/{sid}")
    async def state(sid: str) -> dict[str, Any]:
        return _get(sid).state.to_dict()

    @app.post("/api/session/{sid}/close")
    async def close(sid: str) -> dict[str, Any]:
        """结束会话 —— 未揭示则拒绝（教学伦理红线）。"""
        session = _get(sid)
        try:
            session.assert_revealed()
        except RevealRequired as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        SESSIONS.pop(sid, None)
        _LOCKS.pop(sid, None)
        return {"closed": True}

    return app


def cli() -> None:
    load_env()  # 自动读项目根目录的 .env

    parser = argparse.ArgumentParser(description="网络诽谤演练（你替自己辩白）")
    parser.add_argument("--host", default="127.0.0.1", help="默认只听本机；要外访请用 SSH 端口转发")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--stub", action="store_true", help="用假模型，不需要 API key")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印每轮迭代信息")
    args = parser.parse_args()

    import uvicorn

    app = create_app(use_stub=args.stub, verbose=args.verbose)
    mode = "假模型（StubModel，内容为写死示例）" if args.stub else "真实模型"
    print(f"网络诽谤演练启动：http://{args.host}:{args.port}   模型：{mode}")
    if args.host not in ("127.0.0.1", "localhost"):
        print("⚠️ 你正在监听非本机地址。该服务没有鉴权，请确认这符合你的预期。")
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    cli()

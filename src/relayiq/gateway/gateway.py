"""Tool gateway: the only way agents touch clinic systems.

Every call goes through one policy chokepoint:
  * allow-list   - an agent only receives the tools in its spec (scoped specialists)
  * identity     - PHI reads and all writes require a verified patient (name + DOB)
  * confirmation - writes require caller_confirmed=true, set only after the caller says yes
  * idempotency  - the same write with the same arguments runs once per call
  * audit        - every attempt (executed or denied) lands in the automations ledger,
                   with member IDs / DOB / phone numbers masked
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from ..agents.context import CallContext
from ..db import now_iso

Scope = Literal["read", "write", "auth", "control", "external"]
ToolFn = Callable[[CallContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ToolDef:
    name: str
    scope: Scope
    description: str
    args: type[BaseModel]
    fn: ToolFn
    requires_verified: bool = False
    requires_confirmation: bool = False
    minutes_saved: float = 0.0


class GatewayDenied(Exception):
    pass


_MASKS = [
    (re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"), "****-**-**"),
    (re.compile(r"\+?\d{10,11}\b"), lambda m: "***" + m.group(0)[-4:]),
]
_SENSITIVE_KEYS = {"member_id", "date_of_birth", "dob", "phone", "memberId", "dateOfBirth"}


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" + str(v)[-3:] if k in _SENSITIVE_KEYS and v else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        out = obj
        for pat, rep in _MASKS:
            out = pat.sub(rep, out)
        return out
    return obj


class ToolGateway:
    def __init__(self) -> None:
        self.tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        self.tools[tool.name] = tool

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "scope": t.scope, "description": t.description,
             "requires_verified": t.requires_verified,
             "requires_confirmation": t.requires_confirmation}
            for t in self.tools.values()
        ]

    def _ledger(self, ctx: CallContext, agent: str, tool: ToolDef, decision: str,
                args: dict, result: Any, latency_ms: int) -> None:
        ctx.db.execute(
            "INSERT INTO ledger(ts, call_sid, agent, tool, scope, decision, args, result, "
            "minutes_saved, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), ctx.call_sid, agent, tool.name, tool.scope, decision,
             json.dumps(redact(args))[:2000], json.dumps(redact(result))[:2000],
             tool.minutes_saved if decision == "executed" else 0.0, latency_ms),
        )
        ctx.tool_log.append({"agent": agent, "tool": tool.name, "decision": decision,
                             "args": args, "result": result})
        ctx.emit("tool", {"agent": agent, "tool": tool.name, "decision": decision})

    async def invoke(self, ctx: CallContext, agent: str, name: str, args: dict[str, Any]) -> dict:
        tool = self.tools[name]
        started = time.perf_counter()
        try:
            if tool.requires_verified and not ctx.verified:
                raise GatewayDenied(
                    "Identity not verified. Ask for the patient's full name and date of birth "
                    "and call verify_identity first.")
            if tool.requires_confirmation and not args.get("caller_confirmed"):
                raise GatewayDenied(
                    "This action changes the patient's record. Read the details back to the "
                    "caller, get an explicit yes, then call again with caller_confirmed=true.")
            key = None
            if tool.scope == "write":
                key = name + json.dumps({k: v for k, v in args.items() if k != "caller_confirmed"},
                                        sort_keys=True)
                if key in ctx.executed:
                    return json.loads(ctx.executed[key]) | {"note": "already done earlier in this call"}
            result = await tool.fn(ctx, args)
            succeeded = not (isinstance(result, dict) and result.get("ok") is False)
            if key and succeeded:
                ctx.executed[key] = json.dumps(result)
            # "failed" = the tool ran but couldn't do the thing (slot taken, feature not configured)
            self._ledger(ctx, agent, tool, "executed" if succeeded else "failed", args, result,
                         int((time.perf_counter() - started) * 1000))
            return result
        except GatewayDenied as e:
            result = {"ok": False, "denied": str(e)}
            self._ledger(ctx, agent, tool, "denied", args, result,
                         int((time.perf_counter() - started) * 1000))
            return result

    def langchain_tools(self, ctx: CallContext, agent: str, names: list[str]) -> list[StructuredTool]:
        out = []
        for name in names:
            tool = self.tools.get(name)
            if tool is None:
                continue
            schema = tool.args
            if tool.requires_confirmation:
                schema = create_model(
                    f"{tool.args.__name__}Confirmed", __base__=tool.args,
                    caller_confirmed=(bool, Field(False, description=(
                        "true ONLY after reading the details back and the caller said yes"))),
                )

            def make(tname: str):
                async def _run(**kwargs: Any) -> str:
                    res = await self.invoke(ctx, agent, tname, kwargs)
                    return json.dumps(res, default=str)
                return _run

            out.append(StructuredTool.from_function(
                coroutine=make(name), name=name, description=tool.description,
                args_schema=schema))
        return out

"""STDIO MCP Transport — subprocess + JSON-RPC over stdio (line-delimited JSON)

Spawns MCP server as subprocess, communicates via stdin/stdout.
Each JSON-RPC message is a single line of JSON (MCP stdio spec).

Usage:
    transport = StdioTransport(command="python", args=["-m", "my_mcp_server"])
    await transport.connect()
    await transport.initialize()
    tools = await transport.list_tools()
    result = await transport.call_tool("my_tool", {"arg": "value"})
    await transport.disconnect()
"""
from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any

from .base import MCPBaseTransport, MCPTransportError, make_request, make_notification, parse_response


class StdioTransport(MCPBaseTransport):
    """MCP transport over stdio — subprocess + line-delimited JSON-RPC."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 30.0,
    ):
        super().__init__(timeout=timeout)
        self.command = command
        self.args = args or []
        self.env = env
        self.cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Future[dict]] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_lines: list[str] = []

    async def connect(self) -> None:
        if self._connected:
            return
        # Build command list
        if self.args:
            cmd = [self.command, *self.args]
        else:
            # command may contain args (e.g. "python -m server")
            try:
                cmd = shlex.split(self.command)
            except ValueError:
                cmd = [self.command]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                cwd=self.cwd,
            )
        except FileNotFoundError as e:
            raise MCPTransportError(f"stdio command not found: {cmd[0]}: {e}") from e
        except Exception as e:
            raise MCPTransportError(f"failed to spawn stdio process {cmd}: {e}") from e

        self._connected = True
        self._reader_task = asyncio.create_task(self._read_loop())
        # Also drain stderr in background
        asyncio.create_task(self._drain_stderr())

    async def disconnect(self) -> None:
        self._connected = False
        self._initialized = False
        # Cancel reader
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        # Fail pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPTransportError("transport disconnected"))
        self._pending.clear()
        # Terminate process
        if self._proc:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass
            except Exception:
                pass
            self._proc = None

    async def _drain_stderr(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                self._stderr_lines.append(line.decode("utf-8", errors="replace").rstrip())
                # keep last 50
                if len(self._stderr_lines) > 50:
                    self._stderr_lines.pop(0)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _read_loop(self) -> None:
        """Continuously read JSON lines from stdout and resolve pending futures."""
        if not self._proc or not self._proc.stdout:
            return
        try:
            while self._connected:
                line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.timeout + 10)
                if not line:
                    # EOF — process exited
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                # Route to pending future by id
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        # Check for RPC error
                        if "error" in msg and msg["error"] is not None:
                            err = msg["error"]
                            fut.set_exception(MCPTransportError(f"RPC error {err.get('code')}: {err.get('message')}"))
                        else:
                            fut.set_result(msg)
                # Notifications (no id) are ignored for now
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # Fail all pending on reader error
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(MCPTransportError(f"stdio read loop error: {e}"))

    async def _send_line(self, data: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise MCPTransportError("stdio process not connected")
        line = json.dumps(data, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise MCPTransportError(f"stdio write failed (process exited?): {e}; stderr: {'; '.join(self._stderr_lines[-5:])}") from e

    async def send_request(self, method: str, params: dict | None = None) -> dict:
        if not self._connected:
            raise MCPTransportError("not connected — call connect() first")
        msg_id = self._next_id()
        req = make_request(method, params, msg_id)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[msg_id] = fut
        await self._send_line(req)
        try:
            raw = await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(msg_id, None)
            raise MCPTransportError(f"stdio request timeout for {method} (id={msg_id})") from e
        parsed = parse_response(raw)
        # parse_response checks jsonrpc + error; return 'result'
        return parsed.get("result", parsed)

    async def send_notification(self, method: str, params: dict | None = None) -> None:
        if not self._connected:
            return
        notif = make_notification(method, params)
        try:
            await self._send_line(notif)
        except MCPTransportError:
            pass

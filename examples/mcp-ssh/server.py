#!/usr/bin/env python3
"""
MCP mínimo para SSH — ejemplo base para nuevos servidores remotos.

Uso:
    python3 server.py --host=192.168.1.100 --user=ubuntu --key-file=~/.ssh/id_ed25519

Dependencias:
    pip install mcp paramiko
    Requiere: clave pública copiada al servidor con ssh-copy-id
"""

import argparse
import asyncio
import paramiko
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--port", type=int, default=22)
parser.add_argument("--user", required=True)
parser.add_argument("--key-file", default=None)
parser.add_argument("--password", default=None)
args, _ = parser.parse_known_args()

app = Server(f"ssh-{args.host}")


def _connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=args.host, port=args.port, username=args.user, timeout=30)
    if args.key_file:
        kwargs["key_filename"] = args.key_file
    elif args.password:
        kwargs["password"] = args.password
    client.connect(**kwargs)
    return client


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="shell",
            description=f"Ejecuta un comando bash en {args.host} vía SSH.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Comando bash a ejecutar"},
                },
                "required": ["command"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        client = _connect()

        if name == "shell":
            _, stdout, stderr = client.exec_command(arguments["command"], timeout=60)
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            exit_code = stdout.channel.recv_exit_status()
            output = f"[exit {exit_code}]\n{err or out}" if exit_code != 0 else (out or err or "(sin output)")
        else:
            output = f"Tool desconocido: {name}"

        client.close()

    except Exception as e:
        output = f"Error SSH: {e}"

    return [types.TextContent(type="text", text=output)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

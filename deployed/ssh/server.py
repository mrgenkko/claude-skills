#!/usr/bin/env python3
"""MCP server para control de servidores Ubuntu vía SSH."""

import argparse
import asyncio
import stat
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
parser.add_argument("--name", default=None)
args, _ = parser.parse_known_args()

SERVER_LABEL = args.name or args.host

app = Server(f"ssh-{SERVER_LABEL}")


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
            description=f"Ejecuta un comando de shell en {SERVER_LABEL} vía SSH.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Comando bash a ejecutar"},
                    "timeout": {"type": "integer", "default": 60, "description": "Timeout en segundos"},
                },
                "required": ["command"],
            },
        ),
        types.Tool(
            name="read_file",
            description=f"Lee el contenido de un archivo en {SERVER_LABEL}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta absoluta del archivo"},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="write_file",
            description=f"Escribe contenido a un archivo en {SERVER_LABEL}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta absoluta del archivo"},
                    "content": {"type": "string", "description": "Contenido a escribir"},
                },
                "required": ["path", "content"],
            },
        ),
        types.Tool(
            name="list_dir",
            description=f"Lista el contenido de un directorio en {SERVER_LABEL}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta del directorio", "default": "/"},
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        client = _connect()

        if name == "shell":
            timeout = arguments.get("timeout", 60)
            _, stdout, stderr = client.exec_command(arguments["command"], timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                output = f"[exit {exit_code}]\n{err or out}"
            else:
                output = out or err or "(sin output)"

        elif name == "read_file":
            sftp = client.open_sftp()
            with sftp.open(arguments["path"], "r") as f:
                output = f.read().decode("utf-8", errors="replace")
            sftp.close()

        elif name == "write_file":
            sftp = client.open_sftp()
            with sftp.open(arguments["path"], "w") as f:
                f.write(arguments["content"].encode("utf-8"))
            sftp.close()
            output = f"Archivo escrito: {arguments['path']}"

        elif name == "list_dir":
            path = arguments.get("path", "/")
            sftp = client.open_sftp()
            entries = sftp.listdir_attr(path)
            sftp.close()
            lines = []
            for e in sorted(entries, key=lambda x: x.filename):
                prefix = "d" if stat.S_ISDIR(e.st_mode) else "-"
                lines.append(f"{prefix}  {e.filename}")
            output = "\n".join(lines) or "(directorio vacío)"

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

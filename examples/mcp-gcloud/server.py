#!/usr/bin/env python3
"""
MCP mínimo para gcloud CLI — ejemplo base para nuevos servidores de GCP.

Uso:
    python3 server.py --project=mi-proyecto --region=us-east4 --workdir=/ruta/proyecto

Dependencias:
    pip install mcp
    Requiere: gcloud CLI instalado y autenticado
"""

import argparse
import asyncio
import subprocess
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--project", required=True)
parser.add_argument("--region", required=True)
parser.add_argument("--workdir", required=True)
parser.add_argument("--account", default=None)
args, _ = parser.parse_known_args()

PROJECT = args.project
REGION = args.region
WORKDIR = args.workdir
ACCOUNT = args.account

app = Server(f"gcloud-{PROJECT}")


def _run(cmd: list | str, shell: bool = False) -> str:
    result = subprocess.run(
        cmd, shell=shell, capture_output=True, text=True, cwd=WORKDIR
    )
    out = result.stdout.strip()
    err = result.stderr.strip()
    if result.returncode != 0:
        return f"[exit {result.returncode}]\n{err or out}"
    return out or err or "(sin output)"


def _account_flags() -> list:
    return [f"--account={ACCOUNT}"] if ACCOUNT else []


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="gcloud",
            description="Ejecuta un comando gcloud CLI arbitrario.",
            inputSchema={
                "type": "object",
                "properties": {
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'Ej: ["run", "services", "list"]',
                    }
                },
                "required": ["args"],
            },
        ),
        types.Tool(
            name="shell",
            description="Ejecuta un comando shell en el directorio del proyecto.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Comando shell a ejecutar"},
                },
                "required": ["command"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    account_flags = _account_flags()

    if name == "gcloud":
        output = _run(["gcloud"] + account_flags + arguments["args"])

    elif name == "shell":
        output = _run(arguments["command"], shell=True)

    else:
        output = f"Tool desconocido: {name}"

    return [types.TextContent(type="text", text=output)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

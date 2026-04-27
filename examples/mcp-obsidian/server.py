#!/usr/bin/env python3
"""
MCP mínimo para vault de Obsidian — ejemplo base para leer y escribir notas.

Uso:
    python3 server.py --vault-path /ruta/al/vault

Dependencias:
    pip install mcp
"""

import argparse
import asyncio
import os
import subprocess
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--vault-path", required=True, help="Ruta al vault de Obsidian")
args, _ = parser.parse_known_args()

VAULT = Path(args.vault_path).expanduser().resolve()

app = Server("obsidian")


def _resolve(rel_path: str) -> Path:
    return (VAULT / rel_path.lstrip("/")).resolve()


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="read_note",
            description="Lee el contenido de una nota del vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relativo al vault"},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="write_note",
            description="Crea o reemplaza una nota en el vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        ),
        types.Tool(
            name="list_notes",
            description="Lista archivos .md en una carpeta del vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "default": ""},
                },
            },
        ),
        types.Tool(
            name="delete_note",
            description="Elimina una nota o carpeta entera del vault (recursivo).",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relativo al vault"},
                },
                "required": ["path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "read_note":
            output = _resolve(arguments["path"]).read_text(encoding="utf-8")

        elif name == "write_note":
            p = _resolve(arguments["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(arguments["content"], encoding="utf-8")
            output = f"Guardado: {arguments['path']}"

        elif name == "list_notes":
            folder = arguments.get("folder") or ""
            root = _resolve(folder) if folder else VAULT
            files = []
            for r, _d, fns in os.walk(root, followlinks=True):
                for fn in fns:
                    if fn.endswith(".md"):
                        files.append(os.path.relpath(os.path.join(r, fn), VAULT))
            output = "\n".join(sorted(files)) if files else "(sin notas)"

        elif name == "delete_note":
            import shutil
            target = _resolve(arguments["path"])
            target.relative_to(VAULT)
            if not target.exists():
                target_md = target.with_suffix(".md")
                if target_md.exists():
                    target = target_md
                else:
                    raise FileNotFoundError(f"No existe: {arguments['path']}")
            if target.is_dir():
                shutil.rmtree(target)
                output = f"Carpeta eliminada: {arguments['path']}"
            else:
                target.unlink()
                output = f"Nota eliminada: {arguments['path']}"

        else:
            output = f"Tool desconocido: {name}"

    except Exception as e:
        output = f"Error: {e}"

    return [types.TextContent(type="text", text=output)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

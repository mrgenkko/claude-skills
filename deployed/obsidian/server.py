#!/usr/bin/env python3
"""MCP server para leer y escribir notas en un vault de Obsidian."""

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
    """Resuelve un path relativo al vault, siguiendo symlinks."""
    return (VAULT / rel_path.lstrip("/")).resolve()


def _ensure_md(p: Path) -> Path:
    return p if p.suffix else p.with_suffix(".md")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="read_note",
            description="Lee el contenido de una nota del vault de Obsidian.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relativo al vault (ej: claude-memory/melquiades-mind/user_role.md)",
                    },
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="write_note",
            description="Crea o reemplaza una nota completa en el vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relativo al vault"},
                    "content": {"type": "string", "description": "Contenido completo de la nota"},
                },
                "required": ["path", "content"],
            },
        ),
        types.Tool(
            name="append_note",
            description="Agrega contenido al final de una nota existente, preservando el contenido previo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relativo al vault"},
                    "content": {"type": "string", "description": "Contenido a agregar al final"},
                },
                "required": ["path", "content"],
            },
        ),
        types.Tool(
            name="search_notes",
            description="Busca notas por contenido con grep recursivo en el vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Texto o patrón a buscar"},
                    "folder": {
                        "type": "string",
                        "description": "Subcarpeta donde buscar (opcional, por defecto todo el vault)",
                        "default": "",
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="list_notes",
            description="Lista archivos .md en una carpeta del vault (recursivo).",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Subcarpeta del vault (vacío = raíz del vault)",
                        "default": "",
                    },
                },
            },
        ),
        types.Tool(
            name="get_context",
            description="Devuelve las convenciones del vault: estructura de carpetas, cuándo usar arquitectura/ vs decisiones/, formatos de notas. Llamar siempre antes de crear o buscar notas.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="delete_note",
            description="Elimina una nota o carpeta entera del vault. Si se pasa una carpeta, borra todo su contenido recursivamente.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relativo al vault (ej: lait/proyectos/foo o lait/proyectos/foo.md)",
                    },
                },
                "required": ["path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "read_note":
            p = _ensure_md(_resolve(arguments["path"]))
            output = p.read_text(encoding="utf-8")

        elif name == "write_note":
            p = _ensure_md(_resolve(arguments["path"]))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(arguments["content"], encoding="utf-8")
            output = f"Nota guardada: {arguments['path']}"

        elif name == "append_note":
            p = _ensure_md(_resolve(arguments["path"]))
            if not p.exists():
                raise FileNotFoundError(f"La nota no existe: {arguments['path']}")
            with open(p, "a", encoding="utf-8") as f:
                f.write("\n" + arguments["content"])
            output = f"Contenido agregado a: {arguments['path']}"

        elif name == "search_notes":
            folder = arguments.get("folder") or ""
            search_root = _resolve(folder) if folder else VAULT
            result = subprocess.run(
                ["grep", "-Rli", "--include=*.md", arguments["query"], str(search_root)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            lines = [
                os.path.relpath(line, VAULT)
                for line in result.stdout.strip().splitlines()
                if line
            ]
            output = "\n".join(sorted(lines)) if lines else "(sin resultados)"

        elif name == "list_notes":
            folder = arguments.get("folder") or ""
            search_root = _resolve(folder) if folder else VAULT
            files = []
            for root, _dirs, filenames in os.walk(search_root, followlinks=True):
                for fn in filenames:
                    if fn.endswith(".md"):
                        full = os.path.join(root, fn)
                        files.append(os.path.relpath(full, VAULT))
            output = "\n".join(sorted(files)) if files else "(sin notas)"

        elif name == "delete_note":
            import shutil
            target = _resolve(arguments["path"])
            # Asegurar que el target esté dentro del vault
            target.relative_to(VAULT)
            if not target.exists():
                # Intentar con extensión .md si no existe
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

        elif name == "get_context":
            context_file = VAULT / "CONTEXT.md"
            if context_file.exists():
                output = context_file.read_text(encoding="utf-8")
            else:
                output = "CONTEXT.md no encontrado en la raíz del vault."

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

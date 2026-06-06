#!/usr/bin/env python3
"""MCP server para Redis con soporte de lectura/escritura vía comandos crudos."""

import argparse
import asyncio
import shlex
import redis
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--port", type=int, default=6379)
parser.add_argument("--db", type=int, default=0)
parser.add_argument("--password", default=None)
parser.add_argument("--name", default=None)
# Por defecto FLUSHALL/FLUSHDB están bloqueados: en Redis son irreversibles y
# borran toda la base. Registrar la instancia con --allow-flush para habilitarlos.
parser.add_argument("--allow-flush", action="store_true")
args, _ = parser.parse_known_args()

SERVER_LABEL = args.name or f"{args.host}:{args.port}/{args.db}"

# Comandos destructivos que requieren --allow-flush explícito.
BLOCKED_COMMANDS = {"FLUSHALL", "FLUSHDB"}

app = Server(f"redis-{SERVER_LABEL}")


def get_client() -> redis.Redis:
    return redis.Redis(
        host=args.host,
        port=args.port,
        db=args.db,
        password=args.password,
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=30,
    )


def _format(value) -> str:
    """Aplana la respuesta de Redis (str, int, list, dict) a texto legible."""
    if value is None:
        return "(nil)"
    if isinstance(value, dict):
        return "\n".join(f"{k}: {v}" for k, v in value.items()) or "(empty)"
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        return "\n".join(str(v) for v in items) if items else "(empty list)"
    if isinstance(value, bool):
        return "OK" if value else "(false)"
    return str(value)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    flush_note = " FLUSHALL/FLUSHDB habilitados." if args.allow_flush else " FLUSHALL/FLUSHDB bloqueados en esta instancia."
    return [
        types.Tool(
            name="command",
            description=(
                f"Ejecuta cualquier comando Redis crudo en {SERVER_LABEL} "
                f"(GET, SET, HGETALL, EXPIRE, DEL, INCR, LPUSH...).{flush_note}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Comando Redis completo, ej: 'SET clave valor' o 'HGETALL mihash'",
                    },
                },
                "required": ["command"],
            },
        ),
        types.Tool(
            name="keys",
            description=f"Lista keys de {SERVER_LABEL} por patrón (vía SCAN) con su tipo y TTL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "default": "*", "description": "Patrón glob, ej: 'user:*'"},
                    "limit": {"type": "integer", "default": 100, "description": "Máximo de keys a devolver"},
                },
            },
        ),
        types.Tool(
            name="info",
            description=f"Devuelve INFO de {SERVER_LABEL} (memoria, clientes, persistencia, stats).",
            inputSchema={
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": "Sección opcional: server, memory, clients, persistence, stats, replication, keyspace",
                    },
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        client = get_client()

        if name == "command":
            raw = arguments["command"].strip()
            if not raw:
                return [types.TextContent(type="text", text="Error: comando vacío.")]
            parts = shlex.split(raw)
            verb = parts[0].upper()
            if verb in BLOCKED_COMMANDS and not args.allow_flush:
                output = (
                    f"Bloqueado: '{verb}' está deshabilitado en {SERVER_LABEL}. "
                    f"Re-registra la instancia con --allow-flush en secrets.json para permitirlo."
                )
            else:
                result = client.execute_command(*parts)
                output = _format(result)

        elif name == "keys":
            pattern = arguments.get("pattern", "*")
            limit = arguments.get("limit", 100)
            lines = []
            for key in client.scan_iter(match=pattern, count=200):
                ktype = client.type(key)
                ttl = client.ttl(key)
                ttl_str = "no-expira" if ttl == -1 else (f"{ttl}s" if ttl >= 0 else "?")
                lines.append(f"{key}  [{ktype}]  ttl={ttl_str}")
                if len(lines) >= limit:
                    break
            output = "\n".join(lines) or f"(sin keys para patrón '{pattern}')"

        elif name == "info":
            section = arguments.get("section")
            data = client.info(section) if section else client.info()
            output = _format(data)

        else:
            output = f"Tool desconocido: {name}"

        client.close()

    except Exception as e:
        output = f"Error Redis: {e}"

    return [types.TextContent(type="text", text=output)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

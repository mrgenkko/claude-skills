#!/usr/bin/env python3
"""MCP server para PostgreSQL con soporte completo de lectura y escritura."""

import argparse
import asyncio
import os
import psycopg2
import psycopg2.extras
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--port", type=int, default=5432)
parser.add_argument("--db", required=True)
parser.add_argument("--user", required=True)
# La contraseña llega por env (MCP_PASSWORD, la pone scripts/mcp-launch.py desde el
# llavero del SO) o por flag (legacy: queda visible en `ps aux`). Gana el flag.
parser.add_argument("--password", default=None)
args, _ = parser.parse_known_args()
# pop: que no la herede nada que este proceso lance.
_env_password = os.environ.pop("MCP_PASSWORD", None)
if args.password is None:  # "" (--password=) es un valor válido
    args.password = _env_password
if args.password is None:
    parser.error("falta la contraseña: MCP_PASSWORD en el entorno (scripts/mcp-launch.py) o --password")


def get_conn():
    return psycopg2.connect(
        host=args.host,
        port=args.port,
        dbname=args.db,
        user=args.user,
        password=args.password,
    )


app = Server(f"postgres-{args.db}")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="query",
            description="Ejecuta cualquier SQL: SELECT, INSERT, UPDATE, DELETE, DDL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "Sentencia SQL a ejecutar"},
                    "limit": {"type": "integer", "default": 100, "description": "Máximo de filas a devolver en SELECT"},
                },
                "required": ["sql"],
            },
        ),
        types.Tool(
            name="tables",
            description="Lista todas las tablas del schema público con su tamaño.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="describe",
            description="Describe las columnas de una tabla.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                },
                "required": ["table"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        if name == "query":
            sql = arguments["sql"].strip()
            cur.execute(sql)
            conn.commit()
            if cur.description:
                limit = arguments.get("limit", 100)
                rows = cur.fetchmany(limit)
                cols = [d[0] for d in cur.description]
                lines = ["\t".join(cols)]
                for row in rows:
                    lines.append("\t".join(str(v) if v is not None else "NULL" for v in row))
                output = "\n".join(lines) or "(sin resultados)"
            else:
                output = f"{cur.rowcount} fila(s) afectadas."

        elif name == "tables":
            cur.execute("""
                SELECT table_name,
                       pg_size_pretty(pg_total_relation_size(quote_ident(table_name))) AS size
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name
            """)
            rows = cur.fetchall()
            output = "\n".join(f"{r[0]}  ({r[1]})" for r in rows) or "(sin tablas)"

        elif name == "describe":
            cur.execute("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
            """, (arguments["table"],))
            rows = cur.fetchall()
            output = (
                "\n".join(f"{r[0]}  {r[1]}  nullable={r[2]}  default={r[3]}" for r in rows)
                or f"Tabla '{arguments['table']}' no encontrada."
            )

        else:
            output = f"Tool desconocido: {name}"

        conn.close()

    except Exception as e:
        output = f"Error: {e}"

    return [types.TextContent(type="text", text=output)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

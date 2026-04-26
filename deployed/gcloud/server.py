#!/usr/bin/env python3
"""MCP server para gcloud CLI y comandos de consola."""

import argparse
import subprocess
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--project", required=True)
parser.add_argument("--region", required=True)
parser.add_argument("--workdir", required=True)
parser.add_argument("--account", default=None)
parser.add_argument("--key-file", default=None)
args, _ = parser.parse_known_args()

PROJECT = args.project
REGION = args.region
WORKDIR = args.workdir
ACCOUNT = args.account
KEY_FILE = args.key_file

# Activar service account si se provee key file
if KEY_FILE and ACCOUNT:
    subprocess.run(
        ["gcloud", "auth", "activate-service-account", ACCOUNT, f"--key-file={KEY_FILE}"],
        capture_output=True,
    )

app = Server(f"gcloud-{PROJECT}")


def _account_flags() -> list:
    return [f"--account={ACCOUNT}"] if ACCOUNT else []


def _run(cmd: list | str, shell: bool = False) -> str:
    result = subprocess.run(
        cmd, shell=shell, capture_output=True, text=True, cwd=WORKDIR
    )
    out = result.stdout.strip()
    err = result.stderr.strip()
    if result.returncode != 0:
        return f"[exit {result.returncode}]\n{err or out}"
    return out or err or "(sin output)"


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
                        "description": 'Ej: ["run", "services", "list", "--region=us-east4"]',
                    }
                },
                "required": ["args"],
            },
        ),
        types.Tool(
            name="cloud_run_status",
            description="Estado de todos los servicios Cloud Run del proyecto.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="gcloud_logs",
            description="Logs de un servicio Cloud Run.",
            inputSchema={
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "gz-web, gz-worker, gz-beat, gz-frontend"},
                    "limit": {"type": "integer", "default": 30},
                    "severity": {"type": "string", "description": "ERROR, WARNING, INFO (opcional)"},
                },
                "required": ["service"],
            },
        ),
        types.Tool(
            name="secret_list",
            description="Lista los secrets de Secret Manager.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="secret_get",
            description="Obtiene el valor de un secret.",
            inputSchema={
                "type": "object",
                "properties": {
                    "secret_name": {"type": "string"},
                    "version": {"type": "string", "default": "latest"},
                },
                "required": ["secret_name"],
            },
        ),
        types.Tool(
            name="shell",
            description="Ejecuta un comando de shell en el directorio del proyecto.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Ej: gsutil ls gs://gz-procurement-files"}
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

    elif name == "cloud_run_status":
        output = _run([
            "gcloud", "run", "services", "list",
            f"--region={REGION}", f"--project={PROJECT}",
            "--format=table(name,status.url,status.conditions[0].type,status.latestReadyRevisionName)",
        ] + account_flags)

    elif name == "gcloud_logs":
        service = arguments["service"]
        limit = arguments.get("limit", 30)
        severity = arguments.get("severity", "")
        filter_str = f'resource.type="cloud_run_revision" resource.labels.service_name="{service}"'
        if severity:
            filter_str += f" severity={severity}"
        output = _run([
            "gcloud", "logging", "read", filter_str,
            f"--limit={limit}", f"--project={PROJECT}",
            "--format=value(timestamp,severity,textPayload)",
        ] + account_flags)

    elif name == "secret_list":
        output = _run([
            "gcloud", "secrets", "list",
            f"--project={PROJECT}",
            "--format=table(name,createTime)",
        ] + account_flags)

    elif name == "secret_get":
        output = _run([
            "gcloud", "secrets", "versions", "access",
            arguments.get("version", "latest"),
            f"--secret={arguments['secret_name']}",
            f"--project={PROJECT}",
        ] + account_flags)

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

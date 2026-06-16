#!/usr/bin/env python3
"""
MCP mínimo para SSH — ejemplo base para nuevos servidores remotos.

Expone `shell` + transferencia de archivos (`download_file` / `upload_file`).
La transferencia es vía SFTP disco-a-disco (como scp, pero dentro del MCP): los bytes
nunca pasan por el contexto del modelo, así que sirve para binarios de decenas de MB.

Uso:
    python3 server.py --host=192.168.1.100 --user=ubuntu --key-file=~/.ssh/id_ed25519

Dependencias:
    pip install mcp paramiko
    Requiere: clave pública copiada al servidor con ssh-copy-id
"""

import argparse
import asyncio
import hashlib
import os
import shlex
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
parser.add_argument("--sudo-password", default=None)
parser.add_argument("--download-dir", default="/tmp")
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


def _sha256(path: str) -> str:
    """sha256 de un archivo local leyendo en bloques (no carga todo en RAM)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _remote_sha256(client: paramiko.SSHClient, remote_path: str) -> str | None:
    """sha256 del archivo remoto vía `sha256sum`; None si no se pudo calcular."""
    _, stdout, _ = client.exec_command(f"sha256sum {shlex.quote(remote_path)}", timeout=120)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    if stdout.channel.recv_exit_status() != 0 or not out:
        return None
    return out.split()[0]


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="shell",
            description=f"Ejecuta un comando bash en {args.host} vía SSH. Los comandos con sudo se manejan automáticamente: el servidor inyecta la contraseña vía stdin (sudo -S) sin necesidad de incluirla en el comando.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Comando bash a ejecutar"},
                },
                "required": ["command"],
            },
        ),
        types.Tool(
            name="download_file",
            description=f"Descarga un archivo desde {args.host} a la máquina local vía SFTP (disco-a-disco). Vía correcta para CUALQUIER archivo, incluidos binarios de decenas de MB: los bytes no pasan por el chat, solo se devuelve la ruta local y metadata. Usar en vez de scp.",
            inputSchema={
                "type": "object",
                "properties": {
                    "remote_path": {"type": "string", "description": "Ruta absoluta del archivo en el servidor remoto"},
                    "local_path": {"type": "string", "description": f"Ruta local destino. Si se omite, se usa <download-dir>/<nombre> (download-dir actual: {args.download_dir})"},
                    "verify": {"type": "boolean", "default": False, "description": "Si true, compara el sha256 local contra el sha256sum remoto y reporta verified"},
                },
                "required": ["remote_path"],
            },
        ),
        types.Tool(
            name="upload_file",
            description=f"Sube un archivo local a {args.host} vía SFTP (disco-a-disco). Vía correcta para CUALQUIER archivo, incluidos binarios de decenas de MB: los bytes no pasan por el chat. Crea el directorio remoto destino si no existe. Usar en vez de scp.",
            inputSchema={
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "Ruta del archivo local a subir"},
                    "remote_path": {"type": "string", "description": "Ruta absoluta destino en el servidor remoto"},
                    "verify": {"type": "boolean", "default": False, "description": "Si true, compara el sha256 local contra el sha256sum remoto y reporta verified"},
                },
                "required": ["local_path", "remote_path"],
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

        elif name == "download_file":
            remote_path = arguments["remote_path"]
            local_path = arguments.get("local_path") or os.path.join(
                args.download_dir, os.path.basename(remote_path.rstrip("/"))
            )
            local_path = os.path.abspath(os.path.expanduser(local_path))
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            sftp = client.open_sftp()
            sftp.get(remote_path, local_path)
            sftp.close()
            sha = _sha256(local_path)
            lines = [f"Descargado: {remote_path} → {local_path}", f"bytes: {os.path.getsize(local_path)}", f"sha256: {sha}"]
            if arguments.get("verify"):
                remote_sha = _remote_sha256(client, remote_path)
                lines.append(
                    "verified: desconocido (no se pudo calcular sha256sum remoto)"
                    if remote_sha is None
                    else f"verified: {str(remote_sha == sha).lower()} (remoto {remote_sha})"
                )
            output = "\n".join(lines)

        elif name == "upload_file":
            local_path = os.path.abspath(os.path.expanduser(arguments["local_path"]))
            remote_path = arguments["remote_path"]
            if not os.path.isfile(local_path):
                raise FileNotFoundError(f"No existe el archivo local: {local_path}")
            remote_dir = os.path.dirname(remote_path.rstrip("/"))
            if remote_dir:
                _, mk_out, _ = client.exec_command(f"mkdir -p {shlex.quote(remote_dir)}")
                mk_out.channel.recv_exit_status()  # esperar a que mkdir termine
            sftp = client.open_sftp()
            sftp.put(local_path, remote_path)
            sftp.close()
            sha = _sha256(local_path)
            lines = [f"Subido: {local_path} → {remote_path}", f"bytes: {os.path.getsize(local_path)}", f"sha256: {sha}"]
            if arguments.get("verify"):
                remote_sha = _remote_sha256(client, remote_path)
                lines.append(
                    "verified: desconocido (no se pudo calcular sha256sum remoto)"
                    if remote_sha is None
                    else f"verified: {str(remote_sha == sha).lower()} (remoto {remote_sha})"
                )
            output = "\n".join(lines)

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

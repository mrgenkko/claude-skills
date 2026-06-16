#!/usr/bin/env python3
"""MCP server para control de servidores Ubuntu vía SSH."""

import argparse
import asyncio
import hashlib
import os
import shlex
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
parser.add_argument("--sudo-password", default=None)
parser.add_argument("--download-dir", default="/tmp")
parser.add_argument("--name", default=None)
args, _ = parser.parse_known_args()

SERVER_LABEL = args.name or args.host

# Umbral por encima del cual read_file deja de devolver texto y redirige a download_file.
READ_TEXT_LIMIT = 256 * 1024  # 256 KB

app = Server(f"ssh-{SERVER_LABEL}")


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
            description=f"Ejecuta un comando de shell en {SERVER_LABEL} vía SSH. Los comandos con sudo se manejan automáticamente: el servidor inyecta la contraseña vía stdin (sudo -S) sin necesidad de incluirla en el comando.",
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
            description=f"Lee el contenido de un archivo de texto en {SERVER_LABEL}. Solo para texto: si el archivo es binario o grande (>256 KB) la herramienta redirige a download_file en vez de devolver el contenido.",
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
            description=f"Escribe contenido de texto a un archivo en {SERVER_LABEL}. Solo para texto. Para binarios o archivos grandes usá upload_file.",
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
            name="download_file",
            description=f"Descarga un archivo desde {SERVER_LABEL} a la máquina local vía SFTP (disco-a-disco). Es la vía correcta para CUALQUIER archivo —incluidos binarios de decenas de MB—: los bytes no pasan por el chat, solo se devuelve la ruta local y metadata. Usar esto en vez de read_file/scp para traer binarios.",
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
            description=f"Sube un archivo local a {SERVER_LABEL} vía SFTP (disco-a-disco). Es la vía correcta para CUALQUIER archivo —incluidos binarios de decenas de MB—: los bytes no pasan por el chat. Crea el directorio remoto destino si no existe. Usar esto en vez de write_file/scp para subir binarios.",
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
            command = arguments["command"]
            if args.sudo_password and "sudo" in command and "sudo -S" not in command:
                command = command.replace("sudo", "sudo -S", 1)
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            if args.sudo_password and "sudo -S" in command:
                stdin.write(args.sudo_password + "\n")
                stdin.flush()
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                output = f"[exit {exit_code}]\n{err or out}"
            else:
                output = out or err or "(sin output)"

        elif name == "read_file":
            path = arguments["path"]
            sftp = client.open_sftp()
            size = sftp.stat(path).st_size
            if size > READ_TEXT_LIMIT:
                sftp.close()
                output = (
                    f"Archivo demasiado grande para texto ({size} bytes). "
                    f"Usá download_file(remote_path={path!r}) para traerlo a disco sin pasarlo por el contexto."
                )
            else:
                with sftp.open(path, "r") as f:
                    raw = f.read()
                sftp.close()
                if b"\x00" in raw:
                    output = (
                        f"El archivo parece binario ({size} bytes). "
                        f"Usá download_file(remote_path={path!r}) para traerlo a disco sin pasarlo por el contexto."
                    )
                else:
                    output = raw.decode("utf-8", errors="replace")

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
            size = os.path.getsize(local_path)
            sha = _sha256(local_path)
            lines = [
                f"Descargado: {remote_path} → {local_path}",
                f"bytes: {size}",
                f"sha256: {sha}",
            ]
            if arguments.get("verify"):
                remote_sha = _remote_sha256(client, remote_path)
                if remote_sha is None:
                    lines.append("verified: desconocido (no se pudo calcular sha256sum remoto)")
                else:
                    lines.append(f"verified: {str(remote_sha == sha).lower()} (remoto {remote_sha})")
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
            size = os.path.getsize(local_path)
            sha = _sha256(local_path)
            lines = [
                f"Subido: {local_path} → {remote_path}",
                f"bytes: {size}",
                f"sha256: {sha}",
            ]
            if arguments.get("verify"):
                remote_sha = _remote_sha256(client, remote_path)
                if remote_sha is None:
                    lines.append("verified: desconocido (no se pudo calcular sha256sum remoto)")
                else:
                    lines.append(f"verified: {str(remote_sha == sha).lower()} (remoto {remote_sha})")
            output = "\n".join(lines)

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

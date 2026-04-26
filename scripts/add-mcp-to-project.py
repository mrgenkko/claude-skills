#!/usr/bin/env python3
"""
Registra MCPs en un proyecto de Claude Code (VSCode extension).

Uso:
    python3 "~/Skills/scripts/add-mcp-to-project.py" /ruta/al/proyecto

Por qué es necesario:
    La VSCode extension de Claude Code lee los MCPs por proyecto desde
    ~/.claude.json → projects["/ruta/proyecto"]["mcpServers"].
    Los archivos ~/.claude/mcp.json y ~/.claude/settings.json son ignorados
    por la extensión de VSCode. Hay que registrar los servidores directamente
    en ~/.claude.json para cada proyecto.

Configuración:
    Todos los MCPs y sus credenciales se definen en scripts/secrets.json.
    Copiar scripts/secrets.example.json → scripts/secrets.json y completar.
"""

import json
import sys
import os

CLAUDE_JSON = os.path.expanduser("~/.claude.json")
SKILLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_FILE = os.path.join(SKILLS_DIR, "scripts", "secrets.json")
VENV_PYTHON = os.path.join(SKILLS_DIR, ".venv", "bin", "python")
MCP_SERVERS_DIR = os.path.expanduser("~/.claude/mcp-servers")


def load_secrets() -> dict:
    if not os.path.exists(SECRETS_FILE):
        print(f"ERROR: No se encontró {SECRETS_FILE}")
        print("Copiar scripts/secrets.example.json → scripts/secrets.json y completar.")
        sys.exit(1)
    with open(SECRETS_FILE) as f:
        return json.load(f)


def build_mcp_servers(servers_config: list) -> dict:
    result = {}
    for entry in servers_config:
        name = entry["name"]
        kind = entry["type"]

        if kind == "gcloud":
            args = [
                f"{MCP_SERVERS_DIR}/gcloud/server.py",
                f"--project={entry['project']}",
                f"--region={entry['region']}",
                f"--workdir={entry['workdir']}",
                f"--account={entry['account']}",
            ]
            if entry.get("key_file"):
                args.append(f"--key-file={entry['key_file']}")

        elif kind == "postgres":
            args = [
                f"{MCP_SERVERS_DIR}/postgres/server.py",
                f"--host={entry['host']}",
                f"--port={entry['port']}",
                f"--db={entry['db']}",
                f"--user={entry['user']}",
                f"--password={entry['password']}",
            ]

        elif kind == "ssh":
            server_label = name.removeprefix("ssh-") or entry["host"]
            args = [
                f"{MCP_SERVERS_DIR}/ssh/server.py",
                f"--host={entry['host']}",
                f"--port={entry.get('port', 22)}",
                f"--user={entry['user']}",
                f"--name={server_label}",
            ]
            if entry.get("key_file"):
                args.append(f"--key-file={entry['key_file']}")
            elif entry.get("password"):
                args.append(f"--password={entry['password']}")

        else:
            print(f"WARN: tipo desconocido '{kind}' para '{name}', ignorando.")
            continue

        result[name] = {"type": "stdio", "command": VENV_PYTHON, "args": args, "env": {}}

    return result


def main():
    update_mode = "--update" in sys.argv
    args_clean = [a for a in sys.argv[1:] if a != "--update"]

    secrets = load_secrets()
    MCP_SERVERS = build_mcp_servers(secrets["mcp_servers"])

    if not args_clean:
        print("Uso: python3 add-mcp-to-project.py /ruta/absoluta/al/proyecto [--update]")
        print()
        print("  --update  sobreescribe entradas existentes con los valores de secrets.json")
        print()
        print("Proyectos disponibles en ~/.claude.json:")
        with open(CLAUDE_JSON) as f:
            d = json.load(f)
        for p in sorted(d.get("projects", {}).keys()):
            srv = list(d["projects"][p].get("mcpServers", {}).keys())
            tag = f"  [{', '.join(srv)}]" if srv else ""
            print(f"  {p}{tag}")
        sys.exit(0)

    project_path = os.path.abspath(args_clean[0])

    with open(CLAUDE_JSON) as f:
        d = json.load(f)

    if "projects" not in d:
        d["projects"] = {}

    if project_path not in d["projects"]:
        d["projects"][project_path] = {
            "allowedTools": [],
            "mcpContextUris": [],
            "mcpServers": {},
            "enabledMcpjsonServers": [],
            "disabledMcpjsonServers": [],
            "hasTrustDialogAccepted": False,
            "ignorePatterns": [],
            "projectOnboardingSeenCount": 0
        }

    existing = d["projects"][project_path].get("mcpServers", {})
    added, updated, skipped = [], [], []

    for name, config in MCP_SERVERS.items():
        if name in existing:
            if update_mode:
                existing[name] = config
                updated.append(name)
            else:
                skipped.append(name)
        else:
            existing[name] = config
            added.append(name)

    d["projects"][project_path]["mcpServers"] = existing

    with open(CLAUDE_JSON, "w") as f:
        json.dump(d, f, indent=2)

    print(f"Proyecto: {project_path}")
    if added:
        print(f"  Agregados   : {', '.join(added)}")
    if updated:
        print(f"  Actualizados: {', '.join(updated)}")
    if skipped:
        print(f"  Ya existían : {', '.join(skipped)}")
    print()
    print("Reinicia Claude Code (VSCode) para que carguen los MCPs.")


if __name__ == "__main__":
    main()

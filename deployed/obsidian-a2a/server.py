#!/usr/bin/env python3
"""MCP obsidian-a2a: wrapper del a2a-obsidian-gateway para Claude Code.

Expone write_note, append_note y delete_note como operaciones auditadas
a través del gateway HTTP. Las operaciones de sólo lectura siguen en
el MCP obsidian (obsidian-raw).
"""

import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("obsidian-a2a")

GATEWAY_URL = os.environ.get("A2A_GATEWAY_URL", "http://localhost:7680")
GATEWAY_KEY = os.environ["A2A_GATEWAY_KEY"]
VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/melquiades/ObsidianVault"))

_HEADERS = {"X-Agent-Key": GATEWAY_KEY, "Content-Type": "application/json"}
_TIMEOUT = 60.0  # propose puede tardar por el LLM de clasificación
_KNOWN_VAULTS = {"wiki", "lait", "melquiades"}


def _path_to_vault(obs_path: str) -> tuple[str, str | None]:
    """Convierte un path relativo al ObsidianVault en (vault, project)."""
    parts = obs_path.strip("/").split("/", 1)
    vault = parts[0]
    if vault not in _KNOWN_VAULTS:
        raise ValueError(
            f"Vault desconocido: '{vault}'. Válidos: {', '.join(sorted(_KNOWN_VAULTS))}"
        )
    rest = parts[1] if len(parts) > 1 else ""
    project = None
    if rest.startswith("proyectos/"):
        segs = rest.split("/")
        if len(segs) >= 2:
            project = segs[1]
    return vault, project


def _idempotency_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:36]


def _read_doc_id(obs_path: str) -> str | None:
    """Lee el campo 'id' del frontmatter YAML de un doc del vault."""
    try:
        full = VAULT_ROOT / obs_path.lstrip("/")
        if not full.suffix:
            full = full.with_suffix(".md")
        content = full.read_text(encoding="utf-8", errors="replace")[:2048]
        m = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not m:
            return None
        for line in m.group(1).splitlines():
            if line.startswith("id:"):
                return line.split(":", 1)[1].strip()
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _read_body(obs_path: str) -> str:
    """Lee el contenido completo de un doc del vault."""
    full = VAULT_ROOT / obs_path.lstrip("/")
    if not full.suffix:
        full = full.with_suffix(".md")
    return full.read_text(encoding="utf-8") if full.exists() else ""


@mcp.tool()
async def write_note(path: str, body: str) -> dict:
    """Crea o reemplaza un documento en el vault vía el gateway a2a.

    Garantiza frontmatter canónico, audit trail y commit+push a GitHub.

    Args:
        path: ruta relativa al ObsidianVault (ej. "lait/proyectos/mi-proj/index.md")
        body: contenido completo del documento en Markdown
    """
    vault, project = _path_to_vault(path)
    request_id = str(uuid4())
    target_doc_id = _read_doc_id(path)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        propose_resp = await client.post(
            f"{GATEWAY_URL}/v1/write/propose",
            headers=_HEADERS,
            json={
                "request_id": request_id,
                "intent": f"write_note via MCP: {path}",
                "content": body,
                "source_agent": "mcp-obsidian-a2a",
                "hints": {"target_vault": vault, "project": project},
                "target_doc_id": target_doc_id,
            },
        )
        propose_resp.raise_for_status()
        proposal = propose_resp.json()

        if proposal.get("violations"):
            return {"status": "rejected", "violations": proposal["violations"]}

        ik = _idempotency_key(path, body, request_id)
        apply_resp = await client.post(
            f"{GATEWAY_URL}/v1/write/apply",
            headers=_HEADERS,
            json={"proposal_id": proposal["proposal_id"], "idempotency_key": ik},
        )
        apply_resp.raise_for_status()
        result = apply_resp.json()
        return {
            "status": result["status"],
            "doc_id": proposal["classification"].get("doc_id"),
            "path": result["final_path"],
            "commit": result["commit_sha"],
        }


@mcp.tool()
async def append_note(path: str, content: str) -> dict:
    """Agrega contenido al final de un documento existente vía el gateway a2a.

    Lee el cuerpo actual, concatena el nuevo contenido y hace un write_note completo.

    Args:
        path: ruta relativa al ObsidianVault
        content: contenido a agregar al final
    """
    existing_body = _read_body(path)
    new_body = (existing_body.rstrip() + "\n\n" + content) if existing_body else content
    return await write_note(path, new_body)


@mcp.tool()
async def delete_note(path: str, reason: str = "Borrado vía MCP") -> dict:
    """Borra un documento del vault con audit trail vía el gateway a2a.

    Requiere que el doc tenga 'id' en el frontmatter (lo tienen todos los docs
    creados por el gateway). Docs creados manualmente sin frontmatter no se pueden
    borrar por esta vía — usar obsidian-raw.delete_note directamente.

    Args:
        path: ruta relativa al ObsidianVault
        reason: motivo del borrado (queda en el audit trail)
    """
    doc_id = _read_doc_id(path)
    if not doc_id:
        return {
            "status": "error",
            "message": (
                "No se encontró 'id' en el frontmatter. "
                "El doc puede no estar indexado en el gateway. "
                "Para docs sin frontmatter a2a, usar obsidian-raw.delete_note."
            ),
        }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/write/delete",
            headers=_HEADERS,
            json={
                "request_id": str(uuid4()),
                "doc_id": doc_id,
                "reason": reason,
                "idempotency_key": _idempotency_key(path, doc_id, "delete"),
            },
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    mcp.run()

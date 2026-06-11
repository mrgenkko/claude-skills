#!/usr/bin/env python3
"""MCP obsidian-a2a — cliente HTTP del a2a-obsidian-gateway (ejemplo base).

Wrapper ligero que expone lecturas y escrituras del vault Obsidian a través del
gateway. Reemplaza por completo al MCP `obsidian` raw (acceso directo al filesystem).

- Lecturas (`read_note`, `get_context`, `list_notes`, `search_notes`) consultan el
  gateway → contenido + entidades GraphRAG + documentos relacionados del grafo Neo4j.
- Escrituras (`write_note`, `append_note`, `delete_note`) usan propose+apply →
  frontmatter canónico, audit trail y commit+push a GitHub.
- `add_attachment` copia binarios al vault directamente (no son docs gobernados).

Uso:
    A2A_GATEWAY_URL=http://localhost:7680 \\
    A2A_GATEWAY_KEY=a2a_<KEY> \\
    OBSIDIAN_VAULT=/ruta/al/ObsidianVault \\
    python3 server.py

Dependencias:
    pip install httpx mcp

Ajusta `_KNOWN_VAULTS` a los vaults de tu instalación.
"""

import hashlib
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("obsidian-a2a")

GATEWAY_URL = os.environ.get("A2A_GATEWAY_URL", "http://localhost:7680")
GATEWAY_KEY = os.environ["A2A_GATEWAY_KEY"]
VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", os.path.expanduser("~/ObsidianVault")))

_HEADERS = {"X-Agent-Key": GATEWAY_KEY, "Content-Type": "application/json"}
_TIMEOUT = 60.0  # propose y graphrag pueden tardar por el LLM
_KNOWN_VAULTS = {"wiki", "lait", "melquiades"}


# ── helpers ───────────────────────────────────────────────────────────────────


def _vault_and_relpath(obs_path: str) -> tuple[str, str]:
    """obs_path "lait/proyectos/mi-proj/index.md" → ("lait", "proyectos/mi-proj/index.md")."""
    parts = obs_path.strip("/").split("/", 1)
    vault = parts[0]
    if vault not in _KNOWN_VAULTS:
        raise ValueError(
            f"Vault desconocido: '{vault}'. Válidos: {', '.join(sorted(_KNOWN_VAULTS))}"
        )
    return vault, parts[1] if len(parts) > 1 else ""


def _project(rel_path: str) -> str | None:
    segs = rel_path.split("/")
    if segs and segs[0] == "proyectos" and len(segs) >= 2:
        return segs[1]
    return None


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
    """Lee el contenido completo de un doc del vault desde disco."""
    full = VAULT_ROOT / obs_path.lstrip("/")
    if not full.suffix:
        full = full.with_suffix(".md")
    return full.read_text(encoding="utf-8") if full.exists() else ""


# ── LECTURAS (vía gateway) ──────────────────────────────────────────────────────


@mcp.tool()
async def read_note(path: str) -> dict:
    """Lee un documento del vault con contexto GraphRAG enriquecido.

    Devuelve raw_content + entidades extraídas + documentos relacionados por el grafo.
    Recibes el contexto relevante en una sola llamada (no necesitas búsquedas extra).

    Args:
        path: ruta relativa al ObsidianVault (ej. "lait/proyectos/mi-proj/index.md")
    """
    vault, rel = _vault_and_relpath(path)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{GATEWAY_URL}/v1/read",
            headers=_HEADERS,
            params={"vault": vault, "path": rel},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_context(vault: str) -> dict:
    """Lee el CONTEXT.md del vault. Para vaults de org (lait/melquiades) concatena
    además el CONTEXT.md global de `wiki` (convenciones transversales + portal de la org).

    Llamar SIEMPRE antes de crear o buscar notas.

    Args:
        vault: wiki | lait | melquiades
    """
    if vault not in _KNOWN_VAULTS:
        raise ValueError(
            f"Vault desconocido: '{vault}'. Válidos: {', '.join(sorted(_KNOWN_VAULTS))}"
        )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        portal = await client.get(
            f"{GATEWAY_URL}/v1/read",
            headers=_HEADERS,
            params={"vault": vault, "path": "CONTEXT.md"},
        )
        portal.raise_for_status()
        result = {"portal": portal.json()}
        if vault != "wiki":
            global_ctx = await client.get(
                f"{GATEWAY_URL}/v1/read",
                headers=_HEADERS,
                params={"vault": "wiki", "path": "CONTEXT.md"},
            )
            if global_ctx.status_code == 200:
                result["global"] = global_ctx.json()
        return result


@mcp.tool()
async def list_notes(
    vault: str,
    path_prefix: str = "",
    kind: str = "",
    limit: int = 100,
) -> dict:
    """Lista documentos indexados de un vault (consulta el índice Postgres, no el disco).

    Args:
        vault: wiki | lait | melquiades
        path_prefix: filtro opcional (ej. "proyectos/gz-")
        kind: filtro opcional (index, decision, runbook, concept…)
        limit: máximo de resultados (1-500)
    """
    params: dict = {"vault": vault, "limit": limit}
    if path_prefix:
        params["path_prefix"] = path_prefix
    if kind:
        params["kind"] = kind
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GATEWAY_URL}/v1/list", headers=_HEADERS, params=params)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def search_notes(query: str, vault: str = "all", top_n: int = 8) -> dict:
    """Búsqueda semántica híbrida GraphRAG (vector + grafo + BM25 + reranker).

    Es una PREGUNTA en lenguaje natural, no un grep. Devuelve una respuesta
    sintetizada con evidencia del grafo (evidence_docs/edges). Más potente que
    listar y filtrar — úsalo para "cómo funciona X", "qué decisión se tomó sobre Y".

    Args:
        query: pregunta o descripción en lenguaje natural
        vault: ámbito de búsqueda — wiki | lait | melquiades | all (default all)
        top_n: número de documentos a rerankear (1-20, default 8)
    """
    # El gateway acepta scope ∈ {wiki, lait, melquiades, all}. Pasamos el vault
    # directo para acotar; "all" es el default seguro ante valores desconocidos.
    scope = vault if vault in {"wiki", "lait", "melquiades", "all"} else "all"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/graphrag/query",
            headers=_HEADERS,
            json={
                "request_id": str(uuid4()),
                "question": query,
                "scope": scope,
                "top_n_rerank": top_n,
            },
        )
        resp.raise_for_status()
        return resp.json()


# ── ESCRITURAS (vía gateway: propose + apply) ───────────────────────────────────


@mcp.tool()
async def write_note(path: str, body: str) -> dict:
    """Crea o reemplaza un documento en el vault vía el gateway a2a.

    Garantiza frontmatter canónico, audit trail y commit+push a GitHub. Si el doc
    ya existe (tiene 'id' en frontmatter), hace un update gobernado reutilizando el
    doc_id. No hay edición por str_replace: el `body` es siempre el documento completo.

    Args:
        path: ruta relativa al ObsidianVault (ej. "lait/proyectos/mi-proj/index.md")
        body: contenido completo del documento en Markdown
    """
    vault, rel = _vault_and_relpath(path)
    project = _project(rel)
    request_id = str(uuid4())
    target_doc_id = _read_doc_id(path)

    # org sólo aplica a vaults de organización (no a la wiki transversal).
    hints: dict = {"target_vault": vault, "project": project}
    if vault in {"lait", "melquiades"}:
        hints["org"] = vault

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        propose_resp = await client.post(
            f"{GATEWAY_URL}/v1/write/propose",
            headers=_HEADERS,
            json={
                "request_id": request_id,
                "intent": f"write_note via MCP: {path}",
                "content": body,
                "source_agent": "mcp-obsidian-a2a",
                "hints": hints,
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
            "doc_id": proposal.get("classification", {}).get("doc_id"),
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
    creados por el gateway) y que esté indexado en GraphRAG.

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
                "El doc puede no estar indexado en el gateway."
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


# ── ATTACHMENTS (filesystem directo — no son docs gobernados) ───────────────────


@mcp.tool()
async def add_attachment(
    source_path: str, filename: str, folder: str = "wiki/attachments"
) -> dict:
    """Copia un binario (imagen, PDF) al vault y retorna el wikilink Obsidian.

    Los attachments no pasan por el gateway (no son documentos gobernados): se
    copian directamente al vault en disco.

    Args:
        source_path: ruta absoluta al archivo en el filesystem
        filename: nombre con el que se guardará (ej. "diagrama.png")
        folder: carpeta destino relativa al vault (default "wiki/attachments")
    """
    src = Path(source_path).expanduser().resolve()
    if not src.is_file():
        return {"status": "error", "message": f"No es un archivo: {source_path}"}
    dest_dir = (VAULT_ROOT / folder.lstrip("/")).resolve()
    dest_dir.relative_to(VAULT_ROOT)  # seguridad: dentro del vault
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    shutil.copy2(src, dest)
    rel = os.path.relpath(dest, VAULT_ROOT)
    return {"status": "ok", "path": rel, "wikilink": f"![[{filename}]]"}


if __name__ == "__main__":
    mcp.run()

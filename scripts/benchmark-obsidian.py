#!/usr/bin/env python3
"""
Benchmark: obsidian-raw vs obsidian-a2a

Mide tiempo de operación y tamaño de payload (proxy de tokens Claude) para
las 4 operaciones de escritura, en ambos MCPs.

Uso:
    python3 scripts/benchmark-obsidian.py [--runs N] [--vault /ruta]

Requisitos:
    - Gateway a2a activo en localhost:7680
    - A2A_GATEWAY_KEY en env o en .env junto a este script

Instala: pip install httpx rich python-dotenv
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

try:
    import httpx
    from rich.console import Console
    from rich.table import Table
    from rich import box
except ImportError:
    print("Instalar dependencias: pip install httpx rich")
    sys.exit(1)

# Cargar .env si existe junto al script
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

GATEWAY_URL = os.environ.get("A2A_GATEWAY_URL", "http://localhost:7680")
GATEWAY_KEY = os.environ.get("A2A_GATEWAY_KEY", "")
VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/melquiades/ObsidianVault"))
TEST_PATH_RAW = "melquiades/proyectos/_benchmark_test/index.md"
TEST_PATH_A2A = "melquiades/proyectos/_benchmark_a2a/index.md"
# Contenido que el clasificador mapea a proyectos/ (index de proyecto)
TEST_DOC_CONTENT = """# Proyecto Benchmark Test

Documento de prueba generado por benchmark-obsidian.py para comparar MCPs.

## ¿Qué hace?

Este proyecto prueba la integración entre los MCPs obsidian-raw y obsidian-a2a.

## Stack

- Python
- FastMCP
- httpx

## Acceso

N/A — proyecto de benchmark.
"""
APPEND_CONTENT = "\n## Sección agregada\n\nContenido añadido mediante append_note.\n"

console = Console()


# ── Proxy de tokens ──────────────────────────────────────────────────────────

def approx_tokens(text: str) -> int:
    """Aproximación rápida: ~4 chars / token (heurística GPT/Claude)."""
    return max(1, len(text) // 4)


def tool_call_tokens(tool_name: str, input_payload: dict, output_payload: str) -> dict:
    """
    Estima los tokens que este tool call consume en el contexto de Claude.

    Claude recibe en su contexto:
      - El request del tool call (nombre + argumentos JSON)  → input tokens
      - La respuesta del tool                                → output tokens
    """
    input_text = json.dumps({"tool": tool_name, "input": input_payload})
    total_in = approx_tokens(input_text)
    total_out = approx_tokens(output_payload)
    return {"input": total_in, "output": total_out, "total": total_in + total_out}


# ── Operaciones obsidian-raw (filesystem directo) ────────────────────────────

def raw_write(path: str, content: str) -> tuple[str, float]:
    full = VAULT_ROOT / path.lstrip("/")
    full.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    full.write_text(content, encoding="utf-8")
    elapsed = time.perf_counter() - t0
    response = f"Nota guardada: {path}"
    return response, elapsed


def raw_append(path: str, content: str) -> tuple[str, float]:
    # Claude típicamente necesita read_note primero para saber qué hay
    full = VAULT_ROOT / path.lstrip("/")
    t0 = time.perf_counter()
    # Simula read_note (Claude lo llamaría antes del append)
    existing = full.read_text(encoding="utf-8")
    # Luego append
    with open(full, "a", encoding="utf-8") as f:
        f.write(content)
    elapsed = time.perf_counter() - t0
    response = f"Contenido agregado a: {path}"
    # El read previo mete todo el cuerpo al contexto de Claude
    return response, elapsed, existing


def raw_delete(path: str) -> tuple[str, float]:
    full = VAULT_ROOT / path.lstrip("/")
    t0 = time.perf_counter()
    if full.exists():
        full.unlink()
    elapsed = time.perf_counter() - t0
    return f"Nota eliminada: {path}", elapsed


# ── Operaciones obsidian-a2a (HTTP gateway) ──────────────────────────────────

def _headers() -> dict:
    return {"X-Agent-Key": GATEWAY_KEY, "Content-Type": "application/json"}


def _read_doc_id(path: str) -> str | None:
    try:
        full = VAULT_ROOT / path.lstrip("/")
        content = full.read_text(encoding="utf-8", errors="replace")[:2048]
        m = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not m:
            return None
        for line in m.group(1).splitlines():
            if line.startswith("id:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def a2a_write(path: str, body: str, vault: str = "wiki", project: str | None = None) -> tuple[str, float]:
    target_doc_id = _read_doc_id(path)
    request_id = str(uuid4())
    t0 = time.perf_counter()
    with httpx.Client(timeout=60.0) as client:
        propose = client.post(
            f"{GATEWAY_URL}/v1/write/propose",
            headers=_headers(),
            json={
                "request_id": request_id,
                "intent": f"benchmark write_note: {path}",
                "content": body,
                "source_agent": "mcp-obsidian-a2a",
                "hints": {"target_vault": vault, "project": project},
                "target_doc_id": target_doc_id,
            },
        )
        propose.raise_for_status()
        proposal = propose.json()

        if proposal.get("violations"):
            return json.dumps({"status": "rejected", "violations": proposal["violations"]}), time.perf_counter() - t0

        import hashlib
        ik = hashlib.sha256(f"{path}|{body}|{request_id}".encode()).hexdigest()[:36]
        apply = client.post(
            f"{GATEWAY_URL}/v1/write/apply",
            headers=_headers(),
            json={"proposal_id": proposal["proposal_id"], "idempotency_key": ik},
        )
        apply.raise_for_status()
        result = apply.json()

    elapsed = time.perf_counter() - t0
    response = json.dumps({
        "status": result["status"],
        "doc_id": proposal["classification"].get("doc_id"),
        "path": result["final_path"],
        "commit": result.get("commit_sha", "")[:12],
    })
    return response, elapsed


def a2a_append(path: str, content: str, vault: str = "wiki") -> tuple[str, float]:
    existing = (VAULT_ROOT / path.lstrip("/")).read_text(encoding="utf-8") if (VAULT_ROOT / path.lstrip("/")).exists() else ""
    new_body = (existing.rstrip() + "\n\n" + content) if existing else content
    response, elapsed = a2a_write(path, new_body, vault)
    return response, elapsed


def a2a_delete(path: str) -> tuple[str, float]:
    doc_id = _read_doc_id(path)
    if not doc_id:
        return json.dumps({"status": "error", "message": "sin doc_id en frontmatter"}), 0.0
    t0 = time.perf_counter()
    import hashlib
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{GATEWAY_URL}/v1/write/delete",
            headers=_headers(),
            json={
                "request_id": str(uuid4()),
                "doc_id": doc_id,
                "reason": "benchmark delete test",
                "idempotency_key": hashlib.sha256(f"{path}|{doc_id}|delete".encode()).hexdigest()[:36],
            },
        )
        resp.raise_for_status()
        result = resp.json()
    elapsed = time.perf_counter() - t0
    return json.dumps(result), elapsed


# ── Resultado de una operación ────────────────────────────────────────────────

@dataclass
class OpResult:
    op: str
    mcp: str
    elapsed_ms: float
    response_chars: int
    response_tokens: int          # tokens de la respuesta en contexto Claude
    extra_read_chars: int = 0     # chars de reads previos que Claude necesita leer
    extra_read_tokens: int = 0    # tokens de esos reads previos
    total_context_tokens: int = 0 # total tokens que llegan al contexto de Claude
    ok: bool = True
    note: str = ""

    def __post_init__(self):
        self.total_context_tokens = self.response_tokens + self.extra_read_tokens


# ── Runner principal ──────────────────────────────────────────────────────────

def run_benchmark(runs: int = 3) -> list[OpResult]:
    results = []

    # ── BLOQUE RAW ────────────────────────────────────────────────────────────
    console.print("\n[bold cyan]— obsidian-raw (filesystem directo) —[/bold cyan]")

    for i in range(runs):
        # 1. Create (write nuevo)
        resp, elapsed = raw_write(TEST_PATH_RAW, TEST_DOC_CONTENT)
        toks = tool_call_tokens("write_note", {"path": TEST_PATH_RAW, "content": TEST_DOC_CONTENT}, resp)
        results.append(OpResult("create", "raw", elapsed * 1000, len(resp), toks["output"]))
        console.print(f"  raw create #{i+1}: {elapsed*1000:.0f}ms | resp {len(resp)} chars")

    for i in range(runs):
        # 2. Append (raw: Claude necesita read primero → extra tokens)
        resp, elapsed, existing_body = raw_append(TEST_PATH_RAW, APPEND_CONTENT)
        toks_resp = approx_tokens(resp)
        toks_read = approx_tokens(existing_body)
        r = OpResult("append", "raw", elapsed * 1000, len(resp), toks_resp,
                     extra_read_chars=len(existing_body), extra_read_tokens=toks_read)
        results.append(r)
        console.print(f"  raw append #{i+1}: {elapsed*1000:.0f}ms | resp {len(resp)} + read {len(existing_body)} chars")

    for i in range(runs):
        # 3. Update (write sobre existente — raw: Claude puede necesitar read previo)
        resp, elapsed, existing_body = raw_append(TEST_PATH_RAW, "")  # simula read previo
        resp2, elapsed2 = raw_write(TEST_PATH_RAW, TEST_DOC_CONTENT + "\n## Update\nContenido actualizado.\n")
        total_elapsed = elapsed + elapsed2
        toks_resp = approx_tokens(resp2)
        toks_read = approx_tokens(existing_body)
        r = OpResult("update", "raw", total_elapsed * 1000, len(resp2), toks_resp,
                     extra_read_chars=len(existing_body), extra_read_tokens=toks_read,
                     note="read+write")
        results.append(r)
        console.print(f"  raw update #{i+1}: {total_elapsed*1000:.0f}ms | resp {len(resp2)} + read {len(existing_body)} chars")

    for i in range(runs):
        # 4. Delete
        # Recrear nota antes de borrar
        raw_write(TEST_PATH_RAW, TEST_DOC_CONTENT)
        resp, elapsed = raw_delete(TEST_PATH_RAW)
        toks = approx_tokens(resp)
        results.append(OpResult("delete", "raw", elapsed * 1000, len(resp), toks))
        console.print(f"  raw delete #{i+1}: {elapsed*1000:.0f}ms | resp {len(resp)} chars")

    # ── BLOQUE A2A ────────────────────────────────────────────────────────────
    if not GATEWAY_KEY:
        console.print("\n[bold yellow]GATEWAY_KEY no configurada — saltando tests a2a[/bold yellow]")
        console.print("Exportar: export A2A_GATEWAY_KEY=a2a_<KEY>")
        return results

    console.print("\n[bold green]— obsidian-a2a (HTTP gateway) —[/bold green]")

    # Extraer vault/project del path de prueba a2a
    _a2a_vault, _a2a_project = TEST_PATH_A2A.strip("/").split("/", 1)[0], "benchmark-a2a"

    for i in range(runs):
        # 1. Create
        try:
            resp, elapsed = a2a_write(TEST_PATH_A2A, TEST_DOC_CONTENT,
                                      vault=_a2a_vault, project=_a2a_project)
            toks = approx_tokens(resp)
            results.append(OpResult("create", "a2a", elapsed * 1000, len(resp), toks))
            console.print(f"  a2a create #{i+1}: {elapsed*1000:.0f}ms | resp {len(resp)} chars")
        except Exception as e:
            results.append(OpResult("create", "a2a", 0, 0, 0, ok=False, note=str(e)[:60]))
            console.print(f"  a2a create #{i+1}: [red]ERROR {e}[/red]")

    for i in range(runs):
        # 2. Append (a2a: read server-side, Claude NO necesita read_note antes)
        try:
            resp, elapsed = a2a_append(TEST_PATH_A2A, APPEND_CONTENT, vault=_a2a_vault)
            toks = approx_tokens(resp)
            results.append(OpResult("append", "a2a", elapsed * 1000, len(resp), toks,
                                    note="read server-side"))
            console.print(f"  a2a append #{i+1}: {elapsed*1000:.0f}ms | resp {len(resp)} chars")
        except Exception as e:
            results.append(OpResult("append", "a2a", 0, 0, 0, ok=False, note=str(e)[:60]))
            console.print(f"  a2a append #{i+1}: [red]ERROR {e}[/red]")

    for i in range(runs):
        # 3. Update (a2a: detecta doc_id server-side, Claude no necesita read previo)
        try:
            resp, elapsed = a2a_write(TEST_PATH_A2A,
                                      TEST_DOC_CONTENT + "\n## Update\nContenido actualizado.\n",
                                      vault=_a2a_vault, project=_a2a_project)
            toks = approx_tokens(resp)
            results.append(OpResult("update", "a2a", elapsed * 1000, len(resp), toks,
                                    note="doc_id auto-detectado"))
            console.print(f"  a2a update #{i+1}: {elapsed*1000:.0f}ms | resp {len(resp)} chars")
        except Exception as e:
            results.append(OpResult("update", "a2a", 0, 0, 0, ok=False, note=str(e)[:60]))
            console.print(f"  a2a update #{i+1}: [red]ERROR {e}[/red]")

    for i in range(runs):
        # 4. Delete
        try:
            resp, elapsed = a2a_delete(TEST_PATH_A2A)
            toks = approx_tokens(resp)
            results.append(OpResult("delete", "a2a", elapsed * 1000, len(resp), toks))
            console.print(f"  a2a delete #{i+1}: {elapsed*1000:.0f}ms | resp {len(resp)} chars")
        except Exception as e:
            results.append(OpResult("delete", "a2a", 0, 0, 0, ok=False, note=str(e)[:60]))
            console.print(f"  a2a delete #{i+1}: [red]ERROR {e}[/red]")
        finally:
            # Re-crear para la siguiente vuelta si hace falta
            try:
                a2a_write(TEST_PATH_A2A, TEST_DOC_CONTENT, vault=_a2a_vault, project=_a2a_project)
            except Exception:
                pass

    # Cleanup final
    for path in [TEST_PATH_RAW, TEST_PATH_A2A]:
        full = VAULT_ROOT / path.lstrip("/")
        if full.exists():
            full.unlink()

    return results


# ── Reporte ───────────────────────────────────────────────────────────────────

def report(results: list[OpResult]):
    from statistics import mean

    ops = ["create", "append", "update", "delete"]
    mcps = ["raw", "a2a"]

    # Agrupar por (op, mcp)
    def avg(op, mcp, attr):
        vals = [getattr(r, attr) for r in results if r.op == op and r.mcp == mcp and r.ok]
        return mean(vals) if vals else None

    console.print("\n")

    # ── Tabla latencia ──
    t = Table(title="Latencia promedio (ms)", box=box.ROUNDED, show_lines=True)
    t.add_column("Operación", style="bold")
    t.add_column("raw (ms)", justify="right")
    t.add_column("a2a (ms)", justify="right")
    t.add_column("Δ (a2a/raw)", justify="right")
    for op in ops:
        raw_ms = avg(op, "raw", "elapsed_ms")
        a2a_ms = avg(op, "a2a", "elapsed_ms")
        if raw_ms and a2a_ms:
            ratio = f"{a2a_ms / raw_ms:.1f}×"
            color = "red" if a2a_ms > raw_ms * 1.5 else "yellow"
        else:
            ratio = "N/A"
            color = "dim"
        t.add_row(op,
                  f"{raw_ms:.0f}" if raw_ms else "—",
                  f"[{color}]{a2a_ms:.0f}[/{color}]" if a2a_ms else "—",
                  f"[{color}]{ratio}[/{color}]")
    console.print(t)

    # ── Tabla tokens en contexto Claude ──
    t2 = Table(title="Tokens estimados en contexto de Claude", box=box.ROUNDED, show_lines=True)
    t2.add_column("Operación", style="bold")
    t2.add_column("raw resp (tokens)", justify="right")
    t2.add_column("raw read extra", justify="right")
    t2.add_column("raw TOTAL ctx", justify="right", style="bold")
    t2.add_column("a2a resp (tokens)", justify="right")
    t2.add_column("a2a TOTAL ctx", justify="right", style="bold")
    t2.add_column("Ahorro a2a", justify="right")
    for op in ops:
        raw_resp = avg(op, "raw", "response_tokens")
        raw_read = avg(op, "raw", "extra_read_tokens")
        raw_total = avg(op, "raw", "total_context_tokens")
        a2a_resp = avg(op, "a2a", "response_tokens")
        a2a_total = avg(op, "a2a", "total_context_tokens")

        if raw_total and a2a_total:
            saved = raw_total - a2a_total
            pct = (saved / raw_total) * 100
            color = "green" if pct > 10 else "yellow"
            saving_str = f"[{color}]{saved:+.0f} ({pct:.0f}%)[/{color}]"
        else:
            saving_str = "N/A"

        t2.add_row(
            op,
            f"{raw_resp:.0f}" if raw_resp else "—",
            f"{raw_read:.0f}" if raw_read else "0",
            f"{raw_total:.0f}" if raw_total else "—",
            f"{a2a_resp:.0f}" if a2a_resp else "—",
            f"{a2a_total:.0f}" if a2a_total else "—",
            saving_str,
        )
    console.print(t2)

    # ── Conclusiones ──
    console.print("\n[bold]Conclusiones:[/bold]")
    console.print(
        "• [cyan]Latencia[/cyan]: a2a es ~2-10× más lento (HTTP + LLM de clasificación + git push)."
    )
    console.print(
        "• [green]Tokens en append/update[/green]: a2a ahorra los tokens del read previo "
        "que raw-obsidian normalmente obliga a Claude a hacer. "
        "Para docs medianos (~500 tokens) eso es un ahorro real en el contexto."
    )
    console.print(
        "• [yellow]Tokens en create/delete[/yellow]: prácticamente igual entre ambos MCPs."
    )
    console.print(
        "• [bold]Trade-off[/bold]: "
        "a2a consume más tiempo de pared pero menos tokens de Claude. "
        "Para writes frecuentes o docs grandes, a2a escala mejor en costo de tokens."
    )
    console.print(
        "• [dim]Nota: la respuesta de a2a incluye doc_id + commit SHA → "
        "más útil para trazabilidad aunque ocupa ~10 tokens más que el 'Nota guardada: ...' del raw.[/dim]"
    )


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark obsidian-raw vs obsidian-a2a")
    parser.add_argument("--runs", type=int, default=3, help="Número de repeticiones por operación")
    parser.add_argument("--vault", default=str(VAULT_ROOT), help="Ruta al vault de Obsidian")
    parser.add_argument("--gateway-url", default=GATEWAY_URL)
    parser.add_argument("--gateway-key", default=GATEWAY_KEY)
    args = parser.parse_args()

    VAULT_ROOT_OVERRIDE = Path(args.vault)
    if args.gateway_key:
        GATEWAY_KEY = args.gateway_key

    # Parchear globals
    globals()["VAULT_ROOT"] = Path(args.vault)
    globals()["GATEWAY_URL"] = args.gateway_url
    globals()["GATEWAY_KEY"] = args.gateway_key

    console.print(f"[bold]Benchmark obsidian-raw vs obsidian-a2a[/bold]")
    console.print(f"  vault   : {args.vault}")
    console.print(f"  gateway : {args.gateway_url}")
    console.print(f"  key     : {'✓ configurada' if args.gateway_key else '✗ FALTANTE'}")
    console.print(f"  runs    : {args.runs}")

    results = run_benchmark(runs=args.runs)
    report(results)

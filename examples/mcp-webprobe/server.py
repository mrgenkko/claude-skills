#!/usr/bin/env python3
"""
MCP mínimo para diagnóstico de landings (webprobe) — ejemplo base.

A diferencia de ssh/redis (conexión nueva por llamada), el browser de Playwright es caro
y stateful, así que se mantiene un singleton vivo entre llamadas: el lifecycle es invisible
para el agente (_ensure_page arranca-o-reutiliza, con reconstrucción si el browser murió).

Expone lo esencial para los dos casos de uso:
  · medir el "feel" sin volcar el DOM → veredicto numérico compacto:
      status()      estado del browser (barato: NO lo arranca)
      goto(url)     navegar (arranca el browser solo)
      web_vitals()  LCP/CLS/INP/TBT + verdict
  · validar happy-paths autenticados (login → recorrer un flujo):
      fill / press / click / wait_for / evaluate
    click(force=true) dispara el evento a nivel DOM (dispatchEvent), así atraviesa overlays
    que ganan el hit-test por coordenada — caso típico, un <canvas> WebGL a pantalla completa
    (el force coordenada-based de Playwright NO sirve: entregaría el evento al canvas).
    click/press esperan una navegación async (submit→fetch→route SPA) con poll + early-exit.

El server real (deployed/webprobe) añade: multi-tab + reaper de idle, screenshot/trace,
audit de feel (motion-vs-css), FPS/jank, latencia de botón, entrance/interaction animation,
reduced-motion, type/select_option/set_input_files y los gates allow_headed / allow_interact.

Uso:
    pip install "playwright>=1.49" "mcp>=1.0" && playwright install chromium
    python3 server.py --headless
"""

import argparse
import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from playwright.async_api import async_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--name", default="webprobe")
parser.add_argument("--headless", dest="headless", action="store_true", default=True)
parser.add_argument("--headed", dest="headless", action="store_false")
parser.add_argument("--viewport-w", type=int, default=1440)
parser.add_argument("--viewport-h", type=int, default=900)
parser.add_argument("--nav-timeout-ms", type=int, default=15000)
args, _ = parser.parse_known_args()

app = Server(f"webprobe-{args.name}")


# ── Estado singleton + lifecycle lazy (invisible para el agente) ───────────────
class _State:
    pw = None
    browser = None
    context = None
    page = None


_state = _State()
_lock = asyncio.Lock()


async def _teardown():
    """Cierra todo tragando errores; deja el estado limpio para relanzar."""
    for closer in ("context", "browser", "pw"):
        obj = getattr(_state, closer)
        try:
            if obj:
                await (obj.stop() if closer == "pw" else obj.close())
        except Exception:
            pass
    _state.pw = _state.browser = _state.context = _state.page = None


async def _ensure_page():
    """Garantiza una page viva: arranca Chromium la 1ª vez, reutiliza después y reconstruye
    si murió (crash/OOM). El agente nunca verifica si está activo. (El server real hace el
    liveness fuera del lock para no bloquear su reaper; acá se mantiene simple.)"""
    async with _lock:
        alive = _state.browser is not None and _state.browser.is_connected()
        if alive:
            try:
                await _state.page.evaluate("1")   # ¿la page sigue respondiendo?
            except Exception:
                alive = False
        if not alive:
            await _teardown()
            _state.pw = await async_playwright().start()
            _state.browser = await _state.pw.chromium.launch(headless=args.headless)
            _state.context = await _state.browser.new_context(
                viewport={"width": args.viewport_w, "height": args.viewport_h})
            _state.context.set_default_navigation_timeout(args.nav_timeout_ms)
            _state.page = await _state.context.new_page()
    return _state.page


async def _settle_nav(page, url0, poll_ms=800):
    """Tras una acción, espera una navegación ASYNC (submit → fetch → route SPA/pushState)
    con early-exit en cuanto la URL cambia. Sin esto, una acción que dispara un login async
    se reportaría como 'sin navegación' porque el fetch aún no resolvió. Coste: una acción
    que NO navega paga poll_ms (es el tope, no el típico)."""
    try:
        await page.wait_for_url(lambda u: u != url0, timeout=poll_ms)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("load", timeout=800)
    except Exception:
        pass


# ── JS inyectado: mide server-side y devuelve números, nunca vuelca el DOM ─────
_JS_WEB_VITALS = r"""
(args) => new Promise((resolve) => {
  let lcp = null, cls = 0, tbt = 0, inp = 0;
  try { new PerformanceObserver((l) => { for (const e of l.getEntries()) lcp = Math.round(e.startTime); })
    .observe({ type: 'largest-contentful-paint', buffered: true }); } catch (e) {}
  try { new PerformanceObserver((l) => { for (const e of l.getEntries()) if (!e.hadRecentInput) cls += e.value; })
    .observe({ type: 'layout-shift', buffered: true }); } catch (e) {}
  try { new PerformanceObserver((l) => { for (const e of l.getEntries()) tbt += Math.max(0, e.duration - 50); })
    .observe({ type: 'longtask', buffered: true }); } catch (e) {}
  try { new PerformanceObserver((l) => { for (const e of l.getEntries()) if (e.duration > inp) inp = e.duration; })
    .observe({ type: 'event', buffered: true, durationThreshold: 16 }); } catch (e) {}
  setTimeout(() => resolve({ lcp, cls: +cls.toFixed(3), tbt: Math.round(tbt), inp: inp ? Math.round(inp) : null }), args.settle_ms);
});
"""


# ── Tools ─────────────────────────────────────────────────────────────────────
async def _tool_status(a):
    if _state.browser is None or not _state.browser.is_connected():
        return "webprobe (ejemplo) | running=false"
    try:
        url = f" url={_state.page.url}"
    except Exception:
        url = ""
    return f"webprobe (ejemplo) | running=true headless={str(args.headless).lower()}{url}"


async def _tool_goto(a):
    page = await _ensure_page()
    resp = await page.goto(a["url"], wait_until=a.get("wait_until", "load"))
    return f"ok {resp.status if resp else '?'} {page.url}\ntitle: {await page.title()}"


async def _tool_web_vitals(a):
    page = await _ensure_page()
    r = await page.evaluate(_JS_WEB_VITALS, {"settle_ms": int(a.get("settle_ms", 4000))})
    lcp, cls, inp, tbt = r.get("lcp"), r.get("cls"), r.get("inp"), r.get("tbt")
    bad = []
    if isinstance(lcp, (int, float)) and lcp > 2500:
        bad.append("LCP")
    if isinstance(cls, (int, float)) and cls > 0.1:
        bad.append("CLS")
    if isinstance(inp, (int, float)) and inp > 200:
        bad.append("INP")
    return (f"LCP: {lcp if lcp is not None else 'n/a'}ms (good<2500)\n"
            f"CLS: {cls} (good<0.1)\n"
            f"INP: {inp if inp is not None else 'n/a'}ms (good<200)\n"
            f"TBT: {tbt}ms\n"
            f"verdict: {'ok' if not bad else '+'.join(bad) + '_poor'}")


async def _tool_fill(a):
    page = await _ensure_page()
    loc = page.locator(a["selector"]).nth(int(a.get("nth", 0)))
    await loc.fill(a.get("value", ""), timeout=int(a.get("timeout_ms", 5000)))
    # NO se hace eco del valor (puede ser secreto: contraseña/token) — solo longitud
    return f"fill ok: {a['selector']} ← {len(a.get('value', ''))} chars"


async def _tool_press(a):
    page = await _ensure_page()
    url0 = page.url
    key = a["key"]
    if a.get("selector"):
        await page.locator(a["selector"]).nth(int(a.get("nth", 0))).press(key, timeout=int(a.get("timeout_ms", 5000)))
        where = f" en {a['selector']}"
    else:
        await page.keyboard.press(key)
        where = " (foco actual)"
    await _settle_nav(page, url0)
    msg = f"press '{key}'{where}"
    if page.url != url0:
        msg += f" → navegó: {page.url}"
    return msg


async def _tool_click(a):
    page = await _ensure_page()
    selector = a["selector"]
    nth = int(a.get("nth", 0))
    timeout = int(a.get("timeout_ms", 5000))
    force = bool(a.get("force", False))
    url0 = page.url
    loc = page.locator(selector).nth(nth)
    try:
        if force:
            # click a nivel DOM: ignora overlays que ganan el hit-test por coordenada
            # (ej. un <canvas> WebGL a pantalla completa, aunque esté en z-index negativo).
            await loc.dispatch_event("click", timeout=timeout)
        else:
            await loc.click(timeout=timeout)
    except Exception as e:
        first = str(e).strip().splitlines()[0]
        hint = (" — ¿lo tapa un overlay/canvas? reintentá con force=true, o press('Enter') si es un submit."
                if not force and ("intercept" in str(e) or "Timeout" in first) else "")
        return f"(no se pudo click en '{selector}': {first}{hint})"
    await _settle_nav(page, url0)
    if page.url != url0:
        return f"click ok: {selector}\n→ navegó: {page.url} (title: {await page.title()})"
    return f"click ok: {selector} (sin navegación inmediata; url={page.url} — usá wait_for si esperás algo async)"


async def _tool_wait_for(a):
    page = await _ensure_page()
    selector = a["selector"]
    state = a.get("state", "visible")
    try:
        await page.locator(selector).nth(int(a.get("nth", 0))).wait_for(
            state=state, timeout=int(a.get("timeout_ms", 8000)))
    except Exception:
        return f"(wait_for '{selector}' estado '{state}' no se cumplió; url={page.url})"
    return f"wait_for ok: '{selector}' está {state} (url={page.url})"


async def _tool_evaluate(a):
    page = await _ensure_page()
    expr = a["expression"]
    has_arg = a.get("arg") is not None
    result = await (page.evaluate(expr, a["arg"]) if has_arg else page.evaluate(expr))
    if result is None:
        return "evaluate ok (resultado: null/undefined)"
    try:
        s = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        s = str(result)
    cap = int(a.get("max_len", 4000))
    return s if len(s) <= cap else s[:cap] + f" …[+{len(s) - cap} chars]"


_DISPATCH = {
    "status": _tool_status, "goto": _tool_goto, "web_vitals": _tool_web_vitals,
    "fill": _tool_fill, "press": _tool_press, "click": _tool_click,
    "wait_for": _tool_wait_for, "evaluate": _tool_evaluate,
}


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="status",
            description="Estado del browser (running, headless, url). Barato: NO arranca el browser.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="goto",
            description="Navega a una URL. El browser (singleton) arranca solo si no estaba activo.",
            inputSchema={"type": "object", "properties": {
                "url": {"type": "string"},
                "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle", "commit"], "default": "load"},
            }, "required": ["url"]},
        ),
        types.Tool(
            name="web_vitals",
            description="Core Web Vitals (LCP, CLS, INP, TBT) vía PerformanceObserver + verdict. Devuelve números, no el DOM.",
            inputSchema={"type": "object", "properties": {
                "settle_ms": {"type": "integer", "default": 4000},
            }},
        ),
        types.Tool(
            name="fill",
            description="Escribe un valor en un input (usuario/contraseña/...). NO hace eco del valor (secreto): solo longitud. Selector CSS/`text=`/`role=`.",
            inputSchema={"type": "object", "properties": {
                "selector": {"type": "string"}, "value": {"type": "string"},
                "nth": {"type": "integer", "default": 0}, "timeout_ms": {"type": "integer", "default": 5000},
            }, "required": ["selector", "value"]},
        ),
        types.Tool(
            name="press",
            description="Pulsa una tecla/combo ('Enter' para submit, 'Escape', 'Control+a'). Con selector lo enfoca; sin él va al foco actual. Espera navegación async.",
            inputSchema={"type": "object", "properties": {
                "key": {"type": "string"}, "selector": {"type": "string"},
                "nth": {"type": "integer", "default": 0}, "timeout_ms": {"type": "integer", "default": 5000},
            }, "required": ["key"]},
        ),
        types.Tool(
            name="click",
            description="Click en un elemento (selector CSS/`text=`/`role=`). Reporta navegación (espera la async con poll + early-exit). force=true dispara el click a nivel DOM (dispatchEvent) para targets tapados por un overlay/canvas WebGL.",
            inputSchema={"type": "object", "properties": {
                "selector": {"type": "string"},
                "nth": {"type": "integer", "default": 0},
                "force": {"type": "boolean", "default": False, "description": "click DOM-level (ignora overlays/canvas que ganan el hit-test)"},
                "timeout_ms": {"type": "integer", "default": 5000},
            }, "required": ["selector"]},
        ),
        types.Tool(
            name="wait_for",
            description="Espera que un selector llegue a visible|hidden|attached|detached — sincroniza pasos de un flujo (tras Aplicar, esperar el toast o que el spinner desaparezca).",
            inputSchema={"type": "object", "properties": {
                "selector": {"type": "string"},
                "state": {"type": "string", "enum": ["visible", "hidden", "attached", "detached"], "default": "visible"},
                "nth": {"type": "integer", "default": 0}, "timeout_ms": {"type": "integer", "default": 8000},
            }, "required": ["selector"]},
        ),
        types.Tool(
            name="evaluate",
            description="Ejecuta JS arbitrario y devuelve el resultado (JSON, capado). Escape hatch: sembrar un token y saltar el login en test, leer storage/DOM. `arg` JSON opcional → función (no interpola secretos).",
            inputSchema={"type": "object", "properties": {
                "expression": {"type": "string"},
                "arg": {"description": "argumento JSON-serializable pasado a la función (opcional)"},
                "max_len": {"type": "integer", "default": 4000},
            }, "required": ["expression"]},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        fn = _DISPATCH.get(name)
        output = await fn(arguments or {}) if fn else f"Tool desconocido: {name}"
    except Exception as e:
        msg = str(e)
        if any(s in msg for s in ("Target closed", "has been closed", "disconnected", "browser has been closed")):
            await _teardown()
            output = f"[browser-reset] {name}: el browser se cerró; reintentá, se relanza solo."
        else:
            output = f"Error webprobe ({name}): {msg}"
    return [types.TextContent(type="text", text=str(output))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

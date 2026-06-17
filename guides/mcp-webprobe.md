# Guía: MCP de webprobe (diagnóstico de landings)

MCP custom en Python sobre **Playwright**, mismo patrón que `ssh`/`redis`: un solo
binario (`~/.claude/mcp-servers/webprobe/server.py`) que se registra vía
`secrets.json` + `add-mcp-to-project.py` (o la skill `/mcp-project`). Resuelve dos
dolores al probar landings desde Claude Code:

1. **No gastar contexto** verificando si el browser está activo — el lifecycle es
   invisible (singleton server-side que arranca/reutiliza solo).
2. **Diagnosticar "botones lentos / animaciones que no se sienten al entrar"** — lo
   que ningún MCP de la comunidad (`@playwright/mcp`, Chrome DevTools MCP, etc.) mide:
   FPS/jank, INP, latencia de botón y si la animación de entrada realmente dispara.

## ¿Por qué custom y no `@playwright/mcp` oficial?

El oficial hace navegar/click/snapshot muy bien, pero (a) no mide animaciones/perf,
(b) en sesiones largas acumula accessibility-snapshots verbosos que queman contexto.
`webprobe` devuelve **veredictos numéricos compactos** (`smooth|degraded|janky`,
`slow`, `no_entrance_animation`) en vez de volcar el DOM, y reimplementar
navegar/click es trivial para el scope (tus landings en chromium). Es "lo nuestro" y
encaja con gcloud/postgres/ssh/redis.

## Tools que expone

| Grupo | Tool | Uso |
|---|---|---|
| Sesión | `status` | Estado (running, tabs, url, modo). Barato: **no** arranca el browser. |
| Sesión | `goto` | Navega a una URL (relativa a `--base-url` o completa). Arranca el browser solo. |
| Sesión | `reload`, `set_viewport` | Recargar / cambiar viewport. |
| Sesión | `set_mode` | headless↔headed en runtime (gate `allow_headed`). |
| Pestañas | `open_tab`, `list_tabs`, `switch_tab`, `close_tab` | Multi-pestaña (comparar variantes lado a lado). |
| Pestañas | `close_browser` | Cierra todo y libera RAM. |
| Inspección | `inspect_buttons` | **Resumen** de un DS: `{total, warns, signatures[], offenders[]}` — agrupa botones idénticos por firma (NO devuelve 153 clones), detalla solo los ofensores (motion+transform o `transition:all` con duración >0). `include_all` para el array completo. |
| Inspección | `query`, `get_computed_style`, `outer_html` | Props clave de un selector / estilos computados acotados / outerHTML sin truncar (depurar qué clase ganó). |
| **Audit** | `audit_motion_transform` | Design system: marca nodos Motion cuyo CSS transiciona `transform`/`all` con duración >0 (el CSS pelea con la animación de Motion). |
| **Audit** | `audit_feel` | **CI de feel multi-ruta**: lista de rutas → tabla consolidada (motion_offenders, btn_warns, INP/CLS/LCP por página). Valida el DS entero de una. |
| **Perf** | `measure_fps` | FPS/jank durante scroll/hover (rAF). `verdict: smooth\|degraded\|janky`. |
| **Perf** | `button_latency` | Latencia click→repaint (INP-like). `nth` para desambiguar; avisa si el click navega. `verdict: good\|ok\|slow`. |
| **Perf** | `long_tasks` | Long tasks / LoAF que bloquean el render + top-3 ofensores. |
| **Perf** | `entrance_animation_check` | ¿La animación de entrada **on-load** dispara o el elemento aparece estático? + reduced-motion (`nth` opcional). |
| **Perf** | `interaction_animation` | Animación por **click/hover** (modal/drawer): todos los ejes (translateX/Y+scale, scale normalizado por tamaño), `settle_ms`+`opacity_settle_ms`, overshoot **con magnitud** (%), `nth` en trigger/target, `reset` (escape/reload/none) para destapar el siguiente. Target **opcional** → mide el propio trigger (hover sobre un botón). Verdict ok/overshoot_leve(>5%)/overshoot_fuerte(>15%)/opacity_incompleta. |
| **Perf** | `web_vitals` | LCP, CLS, INP, TBT. |
| Captura | `screenshot` | PNG: `return=path` (disco, barato) o `return=inline` (base64, cliente remoto). |
| Captura | `record_trace` | Playwright trace (pesado, opt-in) a `--artifact-dir`. |

Todas las tools de inspección/perf aceptan `tab` opcional (default: la activa).

**Selección por texto/rol** (clave para no depender de selectores estructurales frágiles): las tools de acción (`button_latency`, `entrance_animation_check`, `interaction_animation` trigger, `get_computed_style`, `outer_html`) aceptan, además de CSS, los engines de Playwright: `text="Abrir Modal"`, `role=button[name="Guardar"]`, `button:has-text("right")`. "El botón que dice X" es estable ante cambios de layout. (El `target_selector` de `interaction_animation` es la excepción: solo CSS, porque se sondea en vivo.)

## Ciclo de vida del browser (3 capas)

1. **Lazy + invisible:** cada tool llama internamente a `_ensure_page()`, que arranca
   Chromium la 1ª vez y reutiliza después, con liveness real + reconstrucción si murió
   (crash/OOM por R3F/bloom). **El agente nunca verifica si está activo.**
2. **Control explícito:** `open_tab`/`switch_tab`/`close_tab`/`close_browser`.
   `--max-tabs` (default 8) con LRU-evict de la tab idle más vieja.
3. **Red de seguridad (reaper):** cada `--reaper-interval` (30s) cierra tabs idle
   (`--tab-idle-timeout`, 10 min), hace teardown del browser si todo lleva idle
   (`--browser-idle-timeout`, 30 min) y purga artefactos viejos. Si el agente olvida
   cerrar, el reaper limpia. Al cerrar Claude Code, el proceso muere y se lleva el
   Chromium — nunca queda huérfano. Cualquier timeout en `0` desactiva esa capa.

## headless vs headed

`headless` en `secrets.json` es solo el **modo de arranque**. El agente lo cambia en
runtime con `set_mode(headed=true/false)` (hace teardown + relaunch). El gate es la
capacidad `allow_headed` (default `true`): si la ponés en `false` (ej. server sin
display), `set_mode(headed=true)` se rechaza. **Patrón idéntico al `allow_flush` de
redis.** En esta máquina hay WSLg, así que headed abre ventana real.

## Artefactos (screenshots / traces) y transporte

Los artefactos van a `--artifact-dir` propio (default `~/.cache/webprobe/<name>/`),
**no a `/tmp` pelado**, y el reaper los borra por `--artifact-ttl` (1 h) + cap
`--max-artifacts` (LRU). `screenshot(return="path")` devuelve la ruta (token-cheap,
cliente local stdio); `return="inline"` devuelve `ImageContent` base64 que viaja por
el protocolo y sirve si exponés el MCP por HTTP a otra máquina sin disco.

## Configuración en `secrets.json`

```json
{
  "name": "webprobe",
  "type": "webprobe",
  "browser": "chromium",
  "headless": true,
  "allow_headed": true,
  "max_tabs": 8,
  "tab_idle_timeout": 600,
  "browser_idle_timeout": 1800,
  "timeout_ms": 60000
}
```

**Cero credenciales** — solo registro de instancia. Campos opcionales: `base_url`
(para `goto` relativo), `persistent_profile` (conserva auth/cookies),
`artifact_dir`/`artifact_ttl`/`max_artifacts`. La URL la pasa el agente en `goto`, así
que una sola instancia genérica `webprobe` sirve para todos los proyectos.

## Instalación

```bash
bash "/home/melquiades/Mrgenkko Skills/scripts/install-webprobe-mcp.sh"
# → pip install playwright==1.49.0 en el venv del repo
# → playwright install chromium (casi no-op: ya cacheado en ~/.cache/ms-playwright)
# → copia server.py a ~/.claude/mcp-servers/webprobe/
```

## Registrar en un proyecto

```bash
# vía skill
/mcp-project add lait-landing-02 webprobe

# o directo
python3 "/home/melquiades/Mrgenkko Skills/scripts/add-mcp-to-project.py" /ruta/al/proyecto --only webprobe
```

Reiniciar la extensión VSCode para que cargue.

## Config resultante en `~/.claude.json`

```json
"webprobe": {
  "type": "stdio",
  "command": "/home/melquiades/Mrgenkko Skills/.venv/bin/python",
  "args": [
    "/home/melquiades/.claude/mcp-servers/webprobe/server.py",
    "--browser=chromium",
    "--name=webprobe",
    "--headless",
    "--max-tabs=8",
    "--tab-idle-timeout=600",
    "--browser-idle-timeout=1800"
  ],
  "env": {},
  "timeout": 60000
}
```

## Flujo típico de diagnóstico

```
goto("http://localhost:5173/")          # arranca el browser solo
audit_feel(["/", "/pricing"])           # CI de feel: ofensores + INP/CLS/LCP por ruta, de una
inspect_buttons()                       # resumen: total/warns/firmas + solo ofensores
audit_motion_transform()                # smell: Motion vs CSS transition (el bug del FAB)
measure_fps(action="scroll", duration_ms=3000)   # ¿scroll a tirones? (Lenis/pin-scrub)
entrance_animation_check("section.hero h1")      # ¿el reveal on-load dispara?
interaction_animation('text="Abrir Modal"', ".shadow-xl")   # modal: settle/opacity/overshoot (target CSS)
interaction_animation('text="Lift"', event="hover")          # hover sobre el botón (target omitido = self)
button_latency('text="Empezar"')        # latencia click→repaint (text/role estable, no frágil)
web_vitals()                            # LCP/CLS/INP/TBT
set_mode(headed=true)                   # (opcional) ver a ojo con ventana real (WSLg)
```

## Troubleshooting

**`Error (goto): ... net::ERR_CONNECTION_REFUSED`**
→ El dev server del proyecto no está corriendo. Arrancá `npm run dev` y pasá el puerto real.

**`[browser-reset] ...: el browser se cerró; reintentá`**
→ El Chromium murió (lo cerraste, crash, OOM). Es esperado: la siguiente llamada lo relanza solo.

**`set_mode(headed) deshabilitado ... (allow_headed=false)`**
→ La instancia se registró con `allow_headed:false`. Cambiá a `true` en `secrets.json` y re-registrá con `--update`.

**`button_latency: no se capturó latencia`**
→ El click navega a otra página o no dispara repaint. Probá sobre un botón que cambie estado en la misma página.

**El MCP no aparece en Claude Code (VSCode)**
→ Verificar que está en `~/.claude.json` (no en `settings.json`) y reiniciar la extensión. Confirmar que `playwright` está instalado en el venv (`scripts/install-webprobe-mcp.sh`).

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
| Sesión | `save_storage_state`, `load_storage_state` | Persisten/cargan la sesión (cookies+localStorage) a disco para reusarla entre llamadas/arranques → saltar el login. No mutan (sin gate). El server además persiste la sesión en memoria entre recreaciones de context. |
| Sesión | `set_mode` | `headed` (headless↔headed en runtime, gate `allow_headed`) y/o `reduced_motion` (`reduce`\|`no-preference`, emula prefers-reduced-motion sin relanzar — valida la rama `useReducedMotion` del DS). |
| **Interacción** | `click` | Click en un elemento (botón Entrar/Generar/Aplicar/Aprobar). Selector CSS/`text=`/`role=`; `nth` desambigua. Reporta si navegó (URL+title) o cambió estado. **`force=true`** dispara el click a nivel DOM (dispatchEvent) para targets tapados por un overlay/canvas WebGL (ver gotcha abajo). |
| **Interacción** | `fill` | Escribe en un input/textarea (usuario/contraseña/intent/body). Limpia+setea+dispara `input` (React lo capta). **No hace eco del valor** (secreto): solo longitud. |
| **Interacción** | `type` | Teclea tecla-a-tecla (keydown/keyup reales) — para inputs que ignoran `fill` (máscaras, handlers por tecla). `clear`+`delay_ms`. Preferí `fill` salvo que no dispare el framework. |
| **Interacción** | `press` | Pulsa tecla/combo (`Enter` para submit, `Escape`, `Control+a`, `Tab`). Con `selector` la enfoca; sin él va al foco actual. |
| **Interacción** | `select_option` | Elige opción de un `<select>` nativo por value/label/index. Dropdown custom (divs) → `click` para abrir + `click` la opción. |
| **Interacción** | `set_input_files` | Sube archivo(s) a un `<input type=file>` (Adjuntar/subir), sin abrir el picker del SO. Valida que las rutas existan. |
| **Interacción** | `evaluate` | Ejecuta JS arbitrario y devuelve el resultado (JSON, capado). Escape hatch: **sembrar un token y saltar el login** en test, leer storage/DOM, disparar handlers. `arg` JSON opcional → función (no interpola secretos). |
| **Sync** | `wait_for` | Espera que un selector llegue a `visible`/`hidden`/`attached`/`detached` — sincroniza pasos del flujo (tras Aplicar, esperar el toast / que el spinner desaparezca). No muta (no requiere `allow_interact`). |
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
| **Perf** | `interaction_animation` | Animación por **click/hover** (modal/drawer): todos los ejes (translateX/Y+scale, scale normalizado por tamaño), `settle_ms`+`opacity_settle_ms`, overshoot **con magnitud** (%), `nth` en trigger/target, `reset` (escape/reload/none) para destapar el siguiente. Target **opcional** → mide el propio trigger (hover sobre un botón). `target_within_trigger` (CSS relativo al trigger ya resuelto) para transform propagado padre→hijo: sigue a `trigger_nth` sin nth global desalineado. Verdict ok/overshoot_leve(>5%)/overshoot_fuerte(>15%)/opacity_incompleta. |
| **Perf** | `web_vitals` | LCP, CLS, INP, TBT. |
| Captura | `screenshot` | PNG: `return=path` (disco, barato, reporta **dims reales**) o `return=inline` (base64, cliente remoto). `selector` recorta al elemento (un ancestro con `overflow` puede clipear → `full_page` o apuntá al contenedor scrollable). |
| Captura | `record_trace` | Playwright trace (pesado, opt-in) a `--artifact-dir`. |

Todas las tools de inspección/perf aceptan `tab` opcional (default: la activa).

**Selección por texto/rol** (clave para no depender de selectores estructurales frágiles): las tools de acción (`button_latency`, `entrance_animation_check`, `interaction_animation` trigger, `get_computed_style`, `outer_html`) aceptan, además de CSS, los engines de Playwright: `text="Abrir Modal"`, `role=button[name="Guardar"]`, `button:has-text("right")`. "El botón que dice X" es estable ante cambios de layout. (El `target_selector` de `interaction_animation` es la excepción: solo CSS puro, porque se inyecta a `querySelectorAll`; si le pasás un engine no-CSS falla con un mensaje explícito desde el primer intento y sugiere `target_within_trigger`.)

## Primitivas de interacción (happy-paths autenticados)

El resto del MCP mide/inspecciona; estas **mutan la page** para validar un flujo de
punta a punta — pasar el login y recorrer crear → previsualizar → aplicar → aprobar.
(No es un cambio de naturaleza: `button_latency`/`interaction_animation`/`measure_fps`
**ya** disparaban click/pointer events para medir; esto solo lo expone explícito.)

| Tool | Firma | Notas |
|---|---|---|
| `click` | `click(selector, nth?, force?, timeout_ms?)` | auto-wait; reporta navegación (espera la async con poll + early-exit). `force=true` → click DOM (overlays). |
| `fill` | `fill(selector, value, nth?, force?)` | clear+set+`input` event. Solo loguea longitud (secretos). Alias de `value`: `text`. |
| `type` | `type(selector, text, clear?, delay_ms?)` | tecla-a-tecla; fallback de `fill`. Alias de `text`: `value`. |
| `press` | `press(key, selector?, nth?, force?)` | `Enter`/`Escape`/combos; selector opcional. |
| `select_option` | `select_option(selector, value?, label?, index?)` | solo `<select>` nativo. |
| `set_input_files` | `set_input_files(selector, files)` | sube a `<input type=file>`; valida rutas. |
| `evaluate` | `evaluate(expression, arg?, max_len?)` | JS arbitrario; `arg` JSON → función. |
| `wait_for` | `wait_for(selector, state?, nth?)` | sincroniza pasos; **no muta** (sin gate). |

**Gate `allow_interact`** (default `true`, patrón del `allow_headed`): registrá con
`--forbid-interact` (`allow_interact:false` en `secrets.json`) para una instancia
**solo-medición** que rechaza las tools que mutan (no a `wait_for`, que solo espera).
`status()` muestra `interact=true|false`.

### Gotcha: botones bajo un `<canvas>` WebGL (three.js/R3F)

Si la app tiene un fondo WebGL a pantalla completa (`<canvas fixed inset-0>`), `click`
normal **da timeout**: aunque el canvas esté en `-z-10`, gana el hit-test por coordenada
(`elementFromPoint` devuelve el canvas) y Playwright no deja clickear "a ciegas". El
`force` coordenada-based de Playwright **no sirve** (entregaría el evento al canvas → falso
"ok" sin disparar el handler). Por eso **`click(force=true)` dispara el evento a nivel DOM**
(`dispatchEvent`), que ignora el overlay y corre el handler real. Para submits de form,
`press('Enter')` es la alternativa más simple. *(Caso real: el login de focusyn.)*

**Flujo login por formulario** (validado contra focusyn, jun 2026):
```
goto("http://localhost:7418/login")
fill('[aria-label="Usuario"]', "tester")
fill('[aria-label="Contraseña"]', "Test-Focusyn-2026")
press("Enter", '[aria-label="Contraseña"]')     # submit (o click('text="Entrar"', force=true))
wait_for("text=\"Resumen\"")                      # esperar el dashboard
evaluate("() => Object.keys(localStorage)")       # verifica: aparece focusyn.refresh
```

**Atajo: saltar el login sembrando el token** — útil para no teclear credenciales en cada
corrida. El shape depende de cómo guarda la SPA la sesión:
```
# (a) clave plana (ej. focusyn.refresh):
goto("http://localhost:7418/")
evaluate("(t) => localStorage.setItem('focusyn.refresh', t)", arg="<refresh-token>")
reload()                                          # la SPA arranca ya autenticada

# (b) Zustand/Redux-persist (objeto JSON completo bajo UNA clave, ej. 'auth'):
evaluate("(s) => localStorage.setItem('auth', JSON.stringify(s))",
         arg={"state": {"accessToken": "...", "refreshToken": "...", "user": {}}, "version": 0})
reload()

# (c) login programático (fetch al endpoint, sin tocar el form):
evaluate("""async (c) => {
  const r = await fetch(c.url, {method:'POST', headers:{'Content-Type':'application/json'},
                               body: JSON.stringify({email:c.email, password:c.password})}).then(x=>x.json())
  localStorage.setItem('auth', JSON.stringify({state:{accessToken:r.access, refreshToken:r.refresh, user:r.user}, version:0}))
}""", arg={"url":"http://localhost:7500/api/v1/auth/login","email":"admin@local.test","password":"..."})
```
`arg` se pasa como argumento a la función → el secreto no se interpola en el string.

**Persistencia de sesión (no re-loguear en cada paso).** El `localStorage` vive en el
**BrowserContext**, no en la tab. Si el context se recrea (browser-idle 30 min, crash del
Chromium, `set_mode(headed)`), antes se perdía la sesión y `<ProtectedRoute>` rebotaba a
`/login`. Ahora el server **snapshotea cookies+localStorage** (en el reaper + antes de cada
teardown) y **los restaura** al relanzar el context → la sesión sobrevive sola. Para
persistir **a disco / entre reinicios del proceso MCP**: `save_storage_state(path?)` tras
autenticar, y `load_storage_state(path?)` + `goto` para reusarla.

> Validado en vivo contra focusyn (jun 2026): login por form, `click(force=true)` sobre el
> canvas, y seed-token; los tres llevan al dashboard `/` con `focusyn.refresh` en localStorage.

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

## prefers-reduced-motion (a11y)

`set_mode(reduced_motion="reduce")` emula la media query `prefers-reduced-motion: reduce`
sin relanzar el browser (se aplica por page con `emulateMedia`, y `matchMedia` la refleja).
Sirve para validar que el DS respeta la rama `useReducedMotion`: en `reduce`, una animación
gateada debe quedar **estática** (`interaction_animation` reporta `axes: (sin movimiento)` y
`entrance_animation_check` reporta `reduced_motion_active: true`). El patrón de test es
medir baseline (`no-preference`) → `set_mode(reduced_motion="reduce")` → re-medir el mismo
target: la diferencia (ej. `scale Δ0.12` vs `Δ0`) prueba que el componente respeta la
preferencia. El override persiste a tabs nuevas y a un relaunch por cambio de `headed`.
También se puede fijar el modo de arranque con `--reduced-motion reduce` en el registro.

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
  "allow_interact": true,
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
# transform propagado padre→hijo (Motion whileHover en el <a>, medido en su <svg> hijo):
interaction_animation("nav ul li a", trigger_nth=2,
                      target_within_trigger="span svg", event="hover")  # sigue a trigger_nth, sin nth global
button_latency('text="Empezar"')        # latencia click→repaint (text/role estable, no frágil)
web_vitals()                            # LCP/CLS/INP/TBT
# validar la rama reduced-motion del DS (useReducedMotion): debe quedar estático
set_mode(reduced_motion="reduce")                          # emula prefers-reduced-motion sin relanzar
interaction_animation("main span.inline-flex", event="hover")  # mismo target → axes: (sin movimiento)
set_mode(reduced_motion="no-preference")                   # volver a baseline
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

**`goto` devuelve `ok 200` pero `⚠ redirected_to: ...`**
→ La URL **final ≠ la pedida**: un guard de auth (SPA) o un redirect HTTP te movió (típico: ruta protegida → `/login`). El `200` es del documento que sí cargó, pero no estás donde pediste. Autenticá (seed-token / form) y reintentá.

**Las tools de webprobe "no existen" en el primer uso**
→ Es el harness MCP, no webprobe: las tools llegan **diferidas** (hay que cargar su schema con `ToolSearch` antes de invocarlas). Invocarlas a ciegas falla con InputValidationError. Buscá `select:goto,click,fill,...` (o por keyword `webprobe`) y después ya son invocables normalmente.

**`Input validation error: 'text'/'value' is a required property` en fill/type**
→ Resuelto: ambas aceptan los dos nombres (`fill` canónico `value`, `type` canónico `text`, cada una acepta el otro como alias). Requiere webprobe ≥ v0.5.0 — si persiste, el proceso MCP quedó stale (mirá `status()`): reiniciá la ventana de VSCode.

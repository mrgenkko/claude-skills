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
3. **Reproducir el celular lento desde la PC** (v0.5.6) — CPU/red/GPU throttleadas, gesto
   táctil real y auditoría de causas, para encontrar el stutter *antes* del deploy en vez de
   descubrirlo en el dispositivo. Ver "Emular un celular lento".

## ¿Por qué custom y no `@playwright/mcp` oficial?

El oficial hace navegar/click/snapshot muy bien, pero (a) no mide animaciones/perf,
(b) en sesiones largas acumula accessibility-snapshots verbosos que queman contexto.
`webprobe` devuelve **veredictos numéricos compactos** (`smooth|degraded|janky`,
`slow`, `no_entrance_animation`) en vez de volcar el DOM, y reimplementar
navegar/click es trivial para el scope (tus landings, en chromium/firefox/webkit). Es
"lo nuestro" y encaja con gcloud/postgres/ssh/redis.

## Tools que expone

| Grupo | Tool | Uso |
|---|---|---|
| Sesión | `status` | Estado (running, **qué motores están vivos**, tabs con su motor, url, modo). Barato: **no** arranca el browser. |
| Sesión | `goto` | Navega a una URL (relativa a `--base-url` o completa). Arranca el browser solo. **`browser`** elige el motor (`chromium`\|`firefox`\|`webkit`) — los 3 conviven a la vez sin pisarse (ver sección Multi-navegador). **`device`** emula un celular/tablet completo (ver sección Responsive). **`bypass_cache=true`** fuerza fetch de red (hard-load) — tras rebuild del frontend en apps sin hashing de assets. |
| Sesión | `list_devices` | Enumera los presets de dispositivo para `device` (los mismos del device toolbar de DevTools). Sin `filter` = solo nombres (~133); con `filter` (`'iphone'`, `'pixel'`…) = viewport, dpr, mobile, touch y motor emulado. Barato: **no** arranca el browser. |
| Sesión | `reload`, `set_viewport` | Recargar (`bypass_cache=true` = hard-reload, ignora caché) / cambiar **solo el tamaño** del viewport (para móvil de verdad → `device`, ver sección Responsive). |
| Sesión | `save_storage_state`, `load_storage_state` | Persisten/cargan la sesión (cookies+localStorage) a disco para reusarla entre llamadas/arranques → saltar el login. No mutan (sin gate). El server además persiste la sesión en memoria entre recreaciones de context. |
| Sesión | `set_mode` | `headed` (headless↔headed en runtime, gate `allow_headed`) y/o `reduced_motion` (`reduce`\|`no-preference`, emula prefers-reduced-motion sin relanzar — valida la rama `useReducedMotion` del DS). **headed también habilita scrollbars clásicos** (headless da thin/overlay no representativo — ver sección scrollbars). |
| **Interacción** | `click` | Click en un elemento (botón Entrar/Generar/Aplicar/Aprobar). Selector CSS/`text=`/`role=`; `nth` desambigua. Reporta si navegó (URL+title) o cambió estado. **`force=true`** dispara el click a nivel DOM (dispatchEvent) para targets tapados por un overlay/canvas WebGL (ver gotcha abajo). **`position={x,y}`** clickea un offset (px) dentro del elemento (scrollbar/canvas/mapa/slider). |
| **Interacción** | `fill` | Escribe en un input/textarea (usuario/contraseña/intent/body). Limpia+setea+dispara `input` (React lo capta). **No hace eco del valor** (secreto): solo longitud. |
| **Interacción** | `type` | Teclea tecla-a-tecla (keydown/keyup reales) — para inputs que ignoran `fill` (máscaras, handlers por tecla). `clear`+`delay_ms`. Preferí `fill` salvo que no dispare el framework. |
| **Interacción** | `press` | Pulsa tecla/combo (`Enter` para submit, `Escape`, `Control+a`, `Tab`). Con `selector` la enfoca; sin él va al foco actual. |
| **Interacción** | `mouse` | Primitiva de bajo nivel **trusted**: `down`/`move`/`up` por coordenada del viewport (`button` L/M/R). Para construir gestos a mano cuando `drag` no alcanza. |
| **Interacción** | `drag` | Arrastre **trusted** `from`→`to` (cada uno `{x,y}` o `{selector,nth,offset_x,offset_y}`): thumb de scrollbar/slider, drag-to-pan, drag&drop, resize-handles. `steps` = suavidad. Lo que `dispatchEvent` sintético **no** logra. |
| **Interacción** | `scroll` | Scroll por **rueda trusted** (`page.mouse.wheel`, path del compositor: passive listeners/inertia/scroll-horizontal). `delta_x`/`delta_y`; `selector`/`x,y` posa el cursor sobre el contenedor a scrollear. Settle de 2 rAF → al volver, `scrollTop` ya refleja. |
| **Interacción** | `hover` | `mousemove` **trusted** sobre selector o `x,y` para validar comportamientos hover-only (auto-scroll de nombre, tooltip, menú). Desacoplado de la medición de animación. |
| **Interacción** | `select_option` | Elige opción de un `<select>` nativo por value/label/index. Dropdown custom (divs) → `click` para abrir + `click` la opción. |
| **Interacción** | `set_input_files` | Sube archivo(s) a un `<input type=file>` (Adjuntar/subir), sin abrir el picker del SO. Valida que las rutas existan. |
| **Interacción** | `evaluate` | Ejecuta JS arbitrario y devuelve el resultado (JSON, capado). Escape hatch: **sembrar un token y saltar el login** en test, leer storage/DOM, disparar handlers. `arg` JSON opcional → función (no interpola secretos). |
| **Sync** | `wait_for` | Espera que un selector llegue a `visible`/`hidden`/`attached`/`detached` — sincroniza pasos del flujo (tras Aplicar, esperar el toast / que el spinner desaparezca). No muta (no requiere `allow_interact`). |
| Pestañas | `open_tab`, `list_tabs`, `switch_tab`, `close_tab` | Multi-pestaña (comparar variantes lado a lado). `open_tab` acepta **`browser`** (abre la tab en ese motor); `list_tabs` muestra el motor de cada tab. |
| Pestañas | `close_browser` | Cierra navegadores y libera RAM. Sin args cierra **todos** los motores vivos; con **`browser`** cierra solo ese (conserva su sesión para relanzar sin re-login). |
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
| **Celu lento** | `set_device_class` | Emula la **capacidad** (CPU lenta, red móvil, pocos cores, poca RAM): `desktop`\|`flagship`\|`mid`\|`low_end`. El eje que le falta a `device`, que solo emula la forma. Pegajoso por tab; cada clase vive en su tab. Chromium-only (CDP). Ver sección "Emular un celular lento". |
| **Celu lento** | `gpu_info` | Backend gráfico **real** (renderer WebGL + featureStatus). Headless suele caer a SwiftShader **sin avisar** → todo FPS de canvas/WebGL/blur medido así no representa nada. Corrélo antes de creerle a una medición gráfica. |
| **Celu lento** | `calibrate` | Benchmark de CPU del host → multiplicador necesario para cada dispositivo objetivo. Sin esto, "6x" significa cosas distintas en cada máquina y ningún umbral es portable. |
| **Celu lento** | `frame_stats` | Frames dropeados según el **compositor** (trace del browser), no rAF. Más fiel bajo throttling: el contador rAF vive en el main thread ralentizado y compite con la página que mide. Chromium-only. |
| **Celu lento** | `scroll_gesture` | Scroll por **gesto táctil real** (fling del compositor vía CDP), no `window.scrollBy`. Es el path que recorre un dedo en un celu — el que se traba con listeners no-passive. Gate `allow_interact`. |
| **Celu lento** | `mobile_perf_audit` | Las **causas** del jank móvil: listeners touch/wheel no-passive, área de blur/backdrop-filter, animaciones sobre props de layout, `transition:all` global, will-change, megapíxeles de canvas, filtros SVG, imágenes sobredimensionadas. Devuelve hallazgos priorizados con su fix. |
| **Celu lento** | `perf_matrix` | La misma página en varias `device_class` lado a lado, en tabla. Responde de una "¿en qué punto se rompe?". |
| Captura | `screenshot` | PNG: `return=inline` (default, base64, se ve directo en el resultado) o `return=path` (disco, barato, reporta **dims reales**, requiere `Read` aparte para verla). `selector` recorta al elemento (un ancestro con `overflow` puede clipear → `full_page` o apuntá al contenedor scrollable). |
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
| `click` | `click(selector, nth?, force?, position?, timeout_ms?)` | auto-wait; reporta navegación (espera la async con poll + early-exit). `force=true` → click DOM (overlays). `position={x,y}` → offset px dentro del elemento (ignorado con `force`). |
| `fill` | `fill(selector, value, nth?, force?)` | clear+set+`input` event. Solo loguea longitud (secretos). Alias de `value`: `text`. |
| `type` | `type(selector, text, clear?, delay_ms?)` | tecla-a-tecla; fallback de `fill`. Alias de `text`: `value`. |
| `press` | `press(key, selector?, nth?, force?)` | `Enter`/`Escape`/combos; selector opcional. |
| `mouse` | `mouse(action, x?, y?, button?)` | `down`/`move`/`up` trusted por coord viewport. Gestos a mano (down→move…→up). |
| `drag` | `drag(from, to, steps?, button?)` | `from`/`to` = `{x,y}` o `{selector,nth,offset_x,offset_y}`. Arrastre trusted real (scrollbar/slider/pan/dnd). Reporta navegación. |
| `scroll` | `scroll(delta_x?, delta_y?, selector?\|x,y?, nth?)` | wheel trusted; posa el cursor sobre `selector`/`x,y` antes (scrollea ESE contenedor). Settle 2 rAF. |
| `hover` | `hover(selector?\|x,y?, nth?, force?)` | `mousemove` trusted; valida hover-only sin medir animación. |
| `select_option` | `select_option(selector, value?, label?, index?)` | solo `<select>` nativo. |
| `set_input_files` | `set_input_files(selector, files)` | sube a `<input type=file>`; valida rutas. |
| `evaluate` | `evaluate(expression, arg?, max_len?)` | JS arbitrario; `arg` JSON → función. **Contrato `arg`**: llega tal cual deserializado (objeto→objeto; string→string). NO re-`JSON.stringify` un string; para sembrar storage defendé con `typeof s==='string'?s:JSON.stringify(s)`. |
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

## Multi-navegador (chromium + firefox + webkit a la vez)

**Una sola instancia del MCP maneja los 3 motores vivos en paralelo** (desde v0.5.2). No
hace falta registrar `webprobe-firefox` aparte: el agente elige el motor con el parámetro
`browser` (`chromium`\|`firefox`\|`webkit`) en `goto`/`open_tab`. **Cada tab recuerda su
motor** y conviven sin pisarse — sirve para comparar el mismo flujo entre navegadores
(Blink vs Gecko vs WebKit/Safari):

```
goto(url, browser="chromium")    → t1 (chromium)
goto(url, browser="firefox")     → t2 (firefox)   # NO pisa t1
measure_fps(tab="t1")            # mide chromium
measure_fps(tab="t2")            # mide firefox
status()                         # browsers_live=chromium+firefox
close_browser(browser="firefox") # cierra solo firefox; chromium sigue
```

Detalles del modelo:
- **`default_engine`** = el `--browser` de lanzamiento (default `chromium`). Un `goto` **sin**
  `browser` usa la tab activa (cualquier motor) o abre una en el default.
- **Sesión por motor**: el `storage_state` (cookies+localStorage) es independiente por
  navegador — un login en chromium no autentica firefox. `save_storage_state`/`load_storage_state`
  y `close_browser` aceptan `browser` para apuntar a un motor concreto.
- **headed aplica a todos**: `set_mode(headed=true)` relanza headed cada motor vivo.
- **Reaper**: un motor que se queda sin tabs se cierra solo (libera RAM, conserva su sesión);
  el browser-idle global cierra todo.
- **Compatibilidad**: las instancias ya registradas (`--browser=chromium`) siguen igual; el
  multi-browser es aditivo (el `--browser` pasa a ser solo el motor por defecto).

> **WebKit necesita libs del SO** (la primera vez): `sudo <venv>/playwright install-deps webkit`
> (libwoff1, libgstreamer*, libavif, libenchant, libsecret, libmanette). Sin ellas, `goto(...,
> browser="webkit")` devuelve un mensaje claro pidiendo ese comando. Chromium y Firefox ya
> funcionan sin paso extra.

## Responsive: emular un celular (`device`)

**`set_viewport` NO alcanza para probar responsive.** Redimensionar cambia los píxeles, pero
el sitio sigue viendo un desktop: UA de desktop, `matchMedia('(pointer: coarse)')` en `false`,
sin eventos touch, `dpr=1`. Todo lo que ramifique por **detección** de móvil (y no por media
query de ancho) sigue tomando la rama desktop. Medido sobre la misma página:

```
set_viewport(390, 844) → {ua_mobile:false, pointer_coarse:false, ontouchstart:false, dpr:1,  innerWidth:390}
device="Pixel 7"       → {ua_mobile:true,  pointer_coarse:true,  ontouchstart:true,  dpr:2.625}
```

**`device`** (en `goto`/`open_tab`, desde v0.5.3) emula el dispositivo **entero** —
viewport + `device_scale_factor` + `user_agent` + `is_mobile` + `has_touch` — igual que el
device toolbar de DevTools, con los mismos ~133 presets (`list_devices` los enumera):

```
goto(url, browser="webkit", device="iPhone 15 Pro")     # preset
open_tab(device={"preset": "Pixel 7", "viewport": {"w": 412, "h": 1200}})   # preset + override
open_tab(device={"viewport": {"w": 360, "h": 800}, "is_mobile": true,       # custom puro
                 "has_touch": true, "dpr": 3})
goto(url, device="desktop")                             # sin emular (default)
```

**Un device = un context propio ⇒ desktop y mobile conviven vivos**, igual que los motores.
Ese es el punto: comparás sin perder ninguno, en vez de alternar.

```
open_tab(url, label="desk")                                        → t1  desktop
open_tab(url, browser="webkit", device="iPhone 15 Pro", label="mob") → t2  393x659 dpr3 touch
screenshot(tab="t1"); screenshot(tab="t2")   # ambos vivos, sin relanzar nada
```

Detalles del modelo:
- **El UA no cambia el MOTOR.** `device="iPhone 15 Pro"` sobre chromium es Blink diciendo que
  es Safari — el mismo autoengaño que el device toolbar de Chrome. Para un iPhone fiel,
  combinalo con **`browser="webkit"`** (Safari real); para Android, `browser="chromium"`. Si el
  preset y el motor no matchean, el server lo avisa (no lo bloquea).
- **`is_mobile` no existe en firefox** (Playwright lo rechaza): se ignora **con aviso** y el
  resto (viewport/UA/dpr) sí emula. Para móvil real → webkit o chromium.
- **La sesión se comparte entre devices del mismo motor** (`storage_state` es por motor):
  logueás en desktop y el context mobile nace autenticado.
- **`set_viewport` sigue sirviendo** dentro de una tab con device: barrer breakpoints o probar
  alturas/rotación sin perder la emulación (no toca UA/touch/dpr).
- **Los devices sobreviven a las recreaciones de context** (`set_mode(headed)`, crash,
  `load_storage_state`): el iPhone vuelve como iPhone.
- **Reaper**: un device sin tabs cierra su context (libera RAM); el motor sobrevive si le queda
  algún otro device.
- **`--device`** en `secrets.json` fija el device por defecto de la instancia (default: desktop).
  Es opcional: lo normal es que el agente lo pase por tab.
- **Incompatible con `--persistent-profile`** (ese modo abre un único context fijo): el server
  devuelve un mensaje claro sugiriendo otro motor.

> Un móvil real, ante una página **sin `<meta name="viewport">`**, usa un layout viewport de
> ~980px. La emulación es fiel a eso: vas a ver `innerWidth: 980` aunque el device sea de
> 390px. No es un bug del server — es el síntoma de que a la página le falta el meta tag.

## Emular un celular **lento** (`device_class` + `gpu_tier`)

`device` emula la **forma** (viewport/UA/touch) y responde *"¿se ve bien en un celu?"*.
No responde *"¿se **siente** bien?"* — y esa es otra pregunta, con otro eje. Un celular de
gama baja no es un desktop con la ventana chica: tiene ~4-6x menos CPU por core, red con
latencia alta, pocos cores, poca RAM y una GPU muy inferior. Por eso "en responsive todo
perfecto" convive con stutter en producción.

**Son tres ejes independientes** y hay que moverlos por separado:

| Eje | Cómo | Qué jank descubre |
|---|---|---|
| CPU / red / cores / RAM | `device_class` (CDP, runtime) | JS pesado, hydration, layout thrashing, LCP, CLS |
| GPU | `gpu_tier` (flag de **launch** → relanza) | blur, `backdrop-filter`, WebGL, sombras, exceso de capas |
| Gesto | `scroll_gesture` / `action="touch_scroll"` | listeners no-passive, inercia, scroll del compositor |

```
goto(url, device="Pixel 7", device_class="low_end")   # forma + capacidad juntas
perf_matrix(url, device="Pixel 7")                    # desktop/mid/low_end lado a lado
mobile_perf_audit()                                   # ¿y por qué se rompe?
set_mode(gpu_tier="software")                         # GPU pobre (relanza el browser)
```

Clases: `desktop` (sin throttle) · `flagship` (1.5x, wifi) · `mid` (4x, fast_4g, 8 cores) ·
`low_end` (6x, slow_4g, 4 cores, 2GB). Cada clase vive en **su propia tab**, igual que los
devices: podés comparar sin perder ninguna.

### El punto ciego que esto destapa: headless mide **sin GPU**

Chromium headless cae a **SwiftShader** (rasterización por CPU) sin decir nada. Medido acá:

```
sin flags        → webgl: unavailable_software, gpu_compositing: disabled_software
--use-angle=gl   → 4/4 aceleradas, renderer = ANGLE (AMD ... radeonsi)   ← gpu_tier="hardware"
--use-angle=vulkan → 4/4 aceleradas PERO el contexto WebGL no se crea desde JS  ← inservible
```

O sea: **todas las mediciones gráficas previas a v0.5.6 venían de un backend por software**.
Por eso `gpu_info` existe y `status` muestra el renderer: pedir `hardware` **no garantiza**
obtenerlo, y sin ese dato un número de FPS de canvas/WebGL/blur no es interpretable.

### Por qué `frame_stats` y no solo `measure_fps`

`measure_fps` cuenta con `requestAnimationFrame`, que **vive en el main thread**: bajo 6x de
throttling el propio contador compite con la página que está midiendo. Y el rAF no ve los
frames que pierde el compositor. Medido sobre la misma página y el mismo scroll:

```
measure_fps → avg_fps 59.9, jank 0, verdict smooth
frame_stats → 13/184 frames dropeados (7%), verdict degraded    ← lo que el usuario siente
```

`frame_stats` lee el trace del browser. Detalle de implementación que importa: el tracing es
**del browser entero**, así que con varias tabs vivas los frames de todas caen en el mismo
trace (3 tabs ⇒ "176 fps"). El server siembra un `console.timeStamp` desde la page para
descubrir el pid de su renderer y contar solo los suyos; si no lo logra, lo avisa en vez de
devolver un número inflado.

### Calibración: "6x" no significa lo mismo en cada máquina

```
calibrate() → host_benchmark_index: 2806  (desktop típico: 1000-2000)
              low_end_android (idx≈250)  → cpu 11.2x
              mid_android     (idx≈700)  → cpu 4.0x
              clase low_end (6x fijo) → idx≈468 ≈ low_end_android
```

Sin calibrar, un umbral de FPS que fijes hoy no es reproducible en otra máquina ni en CI.
Si una clase no cae donde querés: `set_device_class(name="low_end", cpu_rate=11.2)`.

### Caso real: lait2 (4 variantes de la misma landing)

Con `device="Pixel 7"` + `device_class="low_end"`, moviendo **solo** el eje GPU:

| Ruta | GPU real | GPU software | Causa (`mobile_perf_audit`) |
|---|---|---|---|
| `/hud` | 0% drop | 1% drop | — |
| `/svg` | 4% drop | 5% drop | 3 listeners no-passive (Lenis) + blur sobre 3.4x el viewport |
| `/blender` | 2% drop | **25% drop** | 2 listeners no-passive + `transition:all` en 142 elementos |
| `/r3f` | 3% drop | **33% drop** | escena WebGL (el canvas ya está capado a dpr 1) |

Lectura: a 6x de CPU las cuatro aguantan. El cuello es **GPU**, y solo aparece al degradarla.
Un `perf_matrix` de las tres clases con GPU real habría dicho "smooth" en todas — de ahí que
los dos ejes tengan que moverse por separado.

### Lo que esto NO simula

Chromium en x86 con throttling **no es** un Snapdragon: no reproduce ARM vs x86, GPU
tile-based, ni el **throttling térmico** tras medio minuto de scroll. Y **iOS/Safari no tiene
CPU throttling** — WebKit no expone CDP, así que ahí solo emulás forma. Sirve para encontrar
el grueso de los problemas antes del deploy y para detectar regresiones; no para números
absolutos que predigan el dispositivo real.

## headless vs headed

`headless` en `secrets.json` es solo el **modo de arranque**. El agente lo cambia en
runtime con `set_mode(headed=true/false)` (hace teardown + relaunch). El gate es la
capacidad `allow_headed` (default `true`): si la ponés en `false` (ej. server sin
display), `set_mode(headed=true)` se rechaza. **Patrón idéntico al `allow_flush` de
redis.** En esta máquina hay WSLg, así que headed abre ventana real.

## Scrollbars: headless NO es representativo

El Chromium **headless no renderiza scrollbars clásicos**: da una barra *thin/overlay* de
~2px que **no reserva espacio** y **no honra `::-webkit-scrollbar`** (verificado en Chromium
131: `offsetWidth - clientWidth` da ~2px aunque el CSS pida `width:14px`; ningún flag headless
lo cambia — probados `--disable-features=OverlayScrollbar[s]`, `FluentOverlayScrollbar`,
`--headless=old`, etc.). Esto produce **falsos negativos** al auditar usabilidad de scroll:
un scrollbar "imposible de agarrar" se ve como inexistente, y medir su ancho/thumb engaña.

**Para auditar scroll/scrollbars usá `set_mode(headed=true)`**: ahí el scrollbar es clásico
(~16px, reserva espacio, agarrable) = lo que ve el usuario real. Requiere display (WSLg en
WSL). `status()` lo refleja: `scrollbars=classic` (headed) vs `scrollbars=overlay(no-repr)`
(headless). El scroll *funcional* sí anda en headless (`scroll`/`measure_fps` scrollean bien);
lo que no es fiable es la **geometría/apariencia** del scrollbar.

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
`--max-artifacts` (LRU). `screenshot(return="inline")` (default) devuelve `ImageContent`
base64 que viaja por el protocolo y se ve directo en el resultado — mejor para inspección
visual, y también lo que corresponde si exponés el MCP por HTTP a otra máquina sin disco.
`return="path"` devuelve la ruta (token-cheap, requiere `Read` aparte para verla); conviene
en corridas largas con muchas capturas donde no hace falta ver cada una.

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
(para `goto` relativo), `device` (dispositivo por defecto, ej. `"iPhone 15 Pro"`; default
desktop — normalmente **no** se fija acá, el agente lo pasa por tab), `viewport_w`/`viewport_h`/`dpr`
(el desktop base), `persistent_profile` (conserva auth/cookies; **incompatible con `device`**),
`artifact_dir`/`artifact_ttl`/`max_artifacts`. La URL la pasa el agente en `goto`, así
que una sola instancia genérica `webprobe` sirve para todos los proyectos.

Desde v0.5.6, además: `device_class` (capacidad por defecto: `desktop`\|`flagship`\|`mid`\|
`low_end`; default `desktop` = sin throttling, comportamiento histórico) y `gpu_tier`
(`auto`\|`hardware`\|`software`; default `auto`). Ambos son **defaults de la instancia** — lo
normal es dejarlos y que el agente los pase por tab / con `set_mode`. Fijar
`gpu_tier: "hardware"` en `secrets.json` tiene sentido si vas a medir WebGL seguido: evita
que las mediciones arranquen en SwiftShader sin que nadie lo note.

## Requisitos

- **venv del repo** (`~/Mrgenkko Skills/.venv`) con `playwright==1.49.0`.
- **Motores de Playwright** en `~/.cache/ms-playwright/`: `chromium`, `firefox`, `webkit` (los baja el instalador).
- **Librerías del SO** (vía `sudo`): chromium/firefox suelen traerlas; **WebKit casi siempre las
  necesita** (`libwoff1`, `libgstreamer*`, `libavif`, `libenchant`, `libsecret`, `libmanette`).
  Instalalas una vez con `sudo "<venv>/bin/playwright" install-deps webkit` (o `install-deps` para
  los 3). Sin ellas, `goto(..., browser="webkit")` devuelve el comando exacto como aviso.
- **Display para headed**: `set_mode(headed=true)` y los motores no-headless requieren un display.
  (Antes de julio 2026 esto corría en WSL y lo proveía WSLg; ahora es Ubuntu nativo.)

### WebKit en Ubuntu 26.04 — dos trampas

Desde la migración a Ubuntu 26.04 (julio 2026), WebKit necesita dos arreglos que **chromium y
firefox no necesitan**. Sin ellos `goto(browser="webkit")` falla; los otros dos motores andan.

**1. `install-deps` no sirve: los sonames cambiaron.** El bundle webkit de Playwright 1.49 se
compiló en Ubuntu 24.04 y linkea contra versiones que 26.04 ya no empaqueta (icu 74 → 78,
libxml2 `.so.2` → `.so.16`, vpx 9 → 12, x264 164 → 165). Por eso apt responde *"no se ha podido
localizar el paquete libicu74"*: no falta, dejó de existir. La solución es traer esas libs de
Noble al `sys/lib` del propio bundle, que sus wrappers ya tienen en `LD_LIBRARY_PATH`:

```bash
scripts/fix-webkit-libs-ubuntu26.sh   # sin sudo, no toca el sistema
```

Reejecutalo después de cada `playwright install webkit`, que borra el bundle y se lleva las libs.

**2. El snap de VSCode rompe el proceso de red.** VSCode está instalado como snap y exporta
`GIO_MODULE_DIR` apuntando a los módulos GIO del snap, linkeados contra la glibc de core20.
El `WPENetworkProcess` de WebKit los carga y muere con `undefined symbol: __libc_pthread_init`;
hacia afuera se ve como `Page.goto: WebKit encountered an internal error`, incluso sobre HTTP
plano. El antídoto es pisar la variable con los módulos del sistema:

```json
"env": { "GIO_MODULE_DIR": "/usr/lib/x86_64-linux-gnu/gio/modules" }
```

`add-mcp-to-project.py` ya lo genera para los MCP de tipo `webprobe`, así que las instancias
nuevas salen sanas. Solo importa si registrás una config a mano.

## Instalación

```bash
bash "/home/melquiades/Mrgenkko Skills/scripts/install-webprobe-mcp.sh"
# → pip install playwright==1.49.0 en el venv del repo
# → playwright install chromium firefox webkit (los 3 motores, en ~/.cache/ms-playwright)
# → playwright install-deps (libs del SO; requiere sudo — si no hay, avisa el comando para webkit)
# → copia server.py a ~/.claude/mcp-servers/webprobe/

# En Ubuntu 26.04, además (ver "WebKit en Ubuntu 26.04" arriba):
bash "/home/melquiades/Mrgenkko Skills/scripts/fix-webkit-libs-ubuntu26.sh"
```

> El MCP es de vida larga: editar `server.py` **no** recarga el proceso vivo. Tras actualizar
> (p. ej. al multi-browser v0.5.2), **recargá la ventana de VSCode** para que las instancias ya
> registradas tomen el server nuevo. `status()` muestra la versión para detectar si quedó stale.

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

**No puedo arrastrar (scrollbar/slider/pan/drag&drop) ni clickear una coordenada precisa**
→ Resuelto en **v0.5.1**: `drag(from,to)` + `mouse(down/move/up)` (eventos trusted, lo que `dispatchEvent` sintético no logra) y `click(position={x,y})` (offset px dentro del elemento). Para scroll por rueda real: `scroll(delta_y=…, selector=…)`. Para hover-only: `hover(…)`.

**Validé el bundle viejo tras reconstruir el frontend**
→ Resuelto en **v0.5.1**: `goto(url, bypass_cache=true)` / `reload(bypass_cache=true)` fuerzan fetch de red ignorando caché (chromium).

**El scrollbar "no existe" / `offsetWidth-clientWidth` da ~0 en headless**
→ No es la app: el headless no renderiza scrollbars clásicos (ver sección *Scrollbars*). Usá `set_mode(headed=true)` para auditarlos. (Documentado en v0.5.1.)

> **Recordá:** un MCP es de vida larga — editar `server.py` **no** recarga el proceso vivo. Tras actualizar a v0.5.1, reiniciá la ventana de VSCode (o el cliente) y confirmá con `status()` que dice `v0.5.1`.

# Guía: MCP de Figma (oficial remoto)

## ¿Qué hace este servidor?

Conecta el agente con Figma via el servidor remoto oficial de Figma.
Permite leer diseños existentes **y** escribir en canvas — crear frames, shapes, componentes, exportar assets, Code Connect.

A diferencia de `Framelink` (solución de comunidad), este servidor es **bidireccional**: diseño → código **y** código → diseño.

## Tipo de conexión

```
Agente (Claude Code / Cursor)
    ←HTTP+OAuth→  https://mcp.figma.com/mcp  (servidor remoto de Figma)
```

No requiere instalar nada localmente ni tener la app de escritorio de Figma abierta.
La autenticación es OAuth — Claude Code almacena el token por nombre de servidor.

## Framelink vs oficial

| Capacidad | Framelink (comunidad) | Oficial remoto |
|---|---|---|
| Leer diseños → código | ✅ (su fuerte, output limpio) | ✅ |
| Escribir en canvas (crear shapes, frames) | ❌ | ✅ |
| Exportar assets via MCP | ❌ | ✅ |
| Code Connect (sync componentes ↔ código) | ❌ | ✅ |
| Auth | PAT por env var | OAuth por nombre |
| Multi-cuenta | ✅ trivial (un nombre por PAT) | ✅ posible (un nombre por cuenta) |

Usar **Framelink** cuando solo se necesita lectura para generar código y se quiere setup mínimo.
Usar el **oficial** cuando se necesita crear assets en Figma o sincronizar componentes.

## Tools disponibles

| Tool | Descripción |
|---|---|
| `get_design_context` | Lee layout y estilos de un nodo de Figma por URL/nodeId |
| `get_screenshot` | Captura imagen de un frame o componente |
| `get_metadata` | Metadata del archivo (nombre, páginas, versión) |
| `use_figma` | Escribe en canvas: crea/modifica frames, shapes, texto, estilos |
| `create_new_file` | Crea un archivo nuevo en Figma |
| `upload_assets` | Sube imágenes al archivo de Figma |
| `get_code_connect_map` | Lee el mapa Code Connect del archivo |
| `add_code_connect_map` | Escribe el mapa Code Connect |
| `get_code_connect_suggestions` | Sugerencias de componentes para conectar al código |
| `get_context_for_code_connect` | Contexto de un componente para generar Code Connect |
| `send_code_connect_mappings` | Publica mappings de Code Connect en Figma |
| `get_figjam` | Lee el contenido de un archivo FigJam |
| `generate_diagram` | Crea un diagrama en FigJam |
| `get_variable_defs` | Lee las variables (tokens) del archivo |
| `search_design_system` | Busca componentes en la librería |
| `get_libraries` | Lista las librerías disponibles del workspace |
| `whoami` | Muestra el usuario autenticado (útil para verificar cuenta activa) |

## Registro en Claude Code (VSCode)

La extensión VSCode lee MCPs por proyecto desde `~/.claude.json`.
Este MCP es de tipo `http`, no usa `add-mcp-to-project.py` (que solo maneja `stdio`).

Editar `~/.claude.json` directamente:

```python
python3 -c "
import json
path = '/home/melquiades/.claude.json'
with open(path) as f:
    d = json.load(f)
d['projects']['/ruta/al/proyecto']['mcpServers']['figma'] = {
    'type': 'http',
    'url': 'https://mcp.figma.com/mcp'
}
with open(path, 'w') as f:
    json.dump(d, f, indent=2)
print('OK')
"
```

O el bloque JSON equivalente dentro del proyecto:

```json
"figma": {
  "type": "http",
  "url": "https://mcp.figma.com/mcp"
}
```

## Autenticación OAuth

Después de registrar el servidor y recargar VSCode:

1. **Recargar la ventana** (Ctrl+Shift+P → "Reload Window")
2. En Claude Code escribir `/mcp`
3. Seleccionar `figma` → **Authenticate**
4. Se abre el browser (en WSL se abre en Windows — funciona sin configuración extra)
5. Autorizar en Figma → volver a Claude Code
6. Confirmar: "Authentication successful. Connected to figma"

### Verificar cuenta activa

```
whoami()  →  devuelve email y nombre del usuario de Figma autenticado
```

## Multi-cuenta

Claude Code guarda **un token OAuth por nombre de servidor**.
Para tener múltiples cuentas de Figma, registrar con nombres distintos apuntando al mismo endpoint:

```json
"figma-personal": {
  "type": "http",
  "url": "https://mcp.figma.com/mcp"
},
"figma-gz": {
  "type": "http",
  "url": "https://mcp.figma.com/mcp"
}
```

Cada nombre tiene su propio flujo OAuth → tokens independientes.
Mismo patrón que `gcloud-gz-dev` / `gcloud-gz-prod`.

Al autenticar `figma-gz`, usar la cuenta del workspace de Grupo Zambrano en el browser.
`whoami()` confirma qué cuenta está activa para cada instancia.

## Registro en Cursor (global)

Archivo: `~/.cursor/mcp.json` — aplica a todos los workspaces.

```json
"figma": {
  "url": "https://mcp.figma.com/mcp",
  "type": "http"
}
```

La autenticación OAuth en Cursor sigue el mismo flujo.
Detalle del formato global: `guides/cursor.md`.

## Skills disponibles en este repo

Antes de llamar `use_figma` o `generate_diagram`, cargar el skill correspondiente desde el proyecto de Claude Code:

| Skill (en el proyecto Figma) | Cuándo usarlo |
|---|---|
| `/figma-use` | Obligatorio antes de `use_figma` |
| `/figma-generate-design` | Traducir un layout o pantalla a Figma |
| `/figma-generate-library` | Construir un sistema de diseño desde código |
| `/figma-generate-diagram` | Obligatorio antes de `generate_diagram` |
| `/figma-code-connect` | Mapear componentes Figma ↔ codebase |

## Uso típico — crear asset en Figma

Flujo para la fase 09c de lait-aioperation-discovery (isométrico en canvas):

```
1. Cargar skill /figma-use
2. use_figma: crear página "Resultados - Assets" en el archivo de Figma
3. use_figma: crear frames "pieza-rack" (240×140) y "nucleo" (300×180)
4. use_figma: dibujar shapes según spec del doc 09c-figma-montaje.md
5. Exportar frames como SVG → frontend/public/models/resultados/
```

## Troubleshooting

| Síntoma | Acción |
|---|---|
| `figma` no aparece en `/mcp` | Verificar entrada en `~/.claude.json` y recargar VSCode |
| "Not authenticated" después de recargar | Hacer el flujo OAuth de nuevo; el token puede haber expirado |
| OAuth no abre el browser en WSL | Verificar que `xdg-open` o el browser por defecto de WSL esté configurado; normalmente funciona automáticamente con WSL2 + Windows |
| `whoami` devuelve cuenta equivocada | Tienes varias instancias `figma-*`; verificar con cuál instancia estás trabajando |
| Rate limit de la API de Figma | Esperar unos segundos; ocurre en archivos muy grandes con muchas requests seguidas |

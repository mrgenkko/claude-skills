# Guía: MCP oficial de Blender Lab

## ¿Qué hace este MCP?

Conecta el agente con una sesión local de **Blender** mediante el **MCP oficial de Blender Lab**.
Sirve para inspeccionar escenas, consultar documentación de la API de Python de Blender y operar sobre la escena desde un cliente MCP compatible.

Esta guía documenta el flujo **oficial** de Blender Lab, no los forks comunitarios que usan nombres parecidos como `blender-mcp`.

## Artefactos oficiales

Proyecto oficial:

- Página del proyecto: [Blender Lab — MCP Server](https://www.blender.org/lab/mcp-server/)
- Código fuente: `bpype/blender_mcp`
- Releases: `projects.blender.org/lab/blender_mcp/releases`

En los releases oficiales aparecen normalmente estos artefactos:

| Artefacto | Uso |
|-----------|-----|
| `blender-<version>.mcpb` | Bundle para clientes que soportan importación directa de `.mcpb` |
| `mcp-<version>.zip` | Paquete del servidor MCP para instalación manual / stdio |
| Add-on / extensión de Blender Lab | Parte que corre dentro de Blender y expone el bridge TCP |

## Tipo de conexión

```text
Agente (Cursor / Claude Code)
    <-stdio o bundle->  servidor MCP oficial `blender-mcp`
    <-TCP->             add-on oficial de Blender Lab
    -> bpy / escena activa de Blender
```

El add-on dentro de Blender expone un socket TCP con `host` y `port` configurables.
El servidor MCP del cliente se conecta a ese socket.

## Diferencia importante con los forks comunitarios

Hay varios proyectos comunitarios publicados como `blender-mcp` o con nombres muy parecidos.
Algunos añaden features útiles, pero también cambian tools, comportamiento y superficie de seguridad.

Para este repo:

- usar siempre releases o código fuente de **Blender Lab**
- no asumir que `uvx blender-mcp` apunta al proyecto oficial
- si instalas un ejecutable `blender-mcp`, verificar su procedencia antes de registrarlo en Cursor o Claude Code

Una señal de alerta: si el servidor muestra referencias a `Poly Haven`, `Rodin`, `telemetry` o tools no mencionadas en Blender Lab, probablemente estás usando un fork comunitario y no el servidor oficial.

## Instalación del lado Blender

1. Instalar y habilitar el add-on / extensión oficial de Blender Lab en Blender
2. Abrir las preferencias del add-on
3. Configurar:
   - `Host`: normalmente `localhost` si Blender y el cliente MCP corren en el mismo sistema / namespace
   - `Port`: normalmente `9876`
   - `Auto Start`: opcional
4. Confirmar en Blender que el servidor está corriendo

Si Blender y el cliente MCP no comparten el mismo `localhost` (por ejemplo Blender en Windows y Claude Code en WSL), `localhost` no basta. Ver la sección de WSL más abajo.

## Instalación del lado cliente

Hay dos rutas válidas:

### Opción A: bundle `.mcpb`

Si el cliente soporta importación de bundles MCP, usar `blender-<version>.mcpb` desde el release oficial.

### Opción B: stdio explícito

Si el cliente necesita un proceso stdio local, instalar el servidor oficial desde el release `mcp-<version>.zip` o desde el código fuente oficial y asegurarte de obtener un ejecutable local `blender-mcp`.

Este repo **no automatiza todavía** la instalación del MCP de Blender Lab.
La recomendación es mantener una instalación fija y conocida en disco, igual que con otros MCPs del ecosistema.

## Registro en Cursor

Archivo: `~/.cursor/mcp.json`

Ejemplo si ya tienes un ejecutable oficial `blender-mcp` instalado localmente:

```json
"blender": {
  "type": "stdio",
  "command": "/ruta/absoluta/al/blender-mcp",
  "args": [],
  "env": {
    "BLENDER_HOST": "localhost",
    "BLENDER_PORT": "9876"
  }
}
```

Notas:

- usar **ruta absoluta** al ejecutable instalado
- mantener `BLENDER_HOST` y `BLENDER_PORT` alineados con el add-on de Blender
- si importas un `.mcpb`, seguir el flujo del cliente y no duplicar otra entrada stdio para el mismo servidor

Tras editar el archivo o importar el bundle: recargar la ventana de Cursor y verificar el estado en `Settings -> MCP`.

## Registro en Claude Code (VSCode)

La extensión VSCode de Claude Code no lee `~/.cursor/mcp.json`.
Registrar el servidor en `~/.claude.json` bajo `projects["/ruta/absoluta"]["mcpServers"]`.

Ejemplo:

```json
"blender": {
  "type": "stdio",
  "command": "/ruta/absoluta/al/blender-mcp",
  "args": [],
  "env": {
    "BLENDER_HOST": "localhost",
    "BLENDER_PORT": "9876"
  }
}
```

De momento `scripts/add-mcp-to-project.py` no gestiona Blender Lab, porque ese script está orientado a los MCPs Python definidos en `scripts/secrets.json`.
Blender se registra manualmente, igual que Lottie.

## Caso mixto Windows + WSL

Si Blender corre en **Windows** y Cursor / Claude Code corren en **WSL**, el `localhost` del cliente MCP no apunta al `localhost` del proceso de Blender.

En ese caso:

1. En Blender, cambiar el `Host` del add-on a una dirección que acepte conexiones desde WSL, por ejemplo `0.0.0.0`
2. Mantener el `Port` en `9876` o el que hayas elegido
3. En el cliente MCP, establecer `BLENDER_HOST` a la IP del host Windows visible desde WSL
4. Reiniciar Blender y el cliente MCP

En muchas instalaciones WSL2, la IP del host Windows coincide con el `nameserver` de `/etc/resolv.conf`, pero conviene verificarlo en tu entorno antes de fijarlo en la configuración.

## Comprobar que funciona

1. Asegurar que Blender está abierto y el add-on oficial indica que el bridge está activo
2. Reiniciar Cursor o Claude Code después de registrar el MCP
3. Hacer una prueba simple en el agente:
   - "lista los objetos de la escena"
   - "describe la escena actual de Blender"
   - "consulta documentación de `bpy.types.Object`"

## Troubleshooting

| Síntoma | Acción |
|---------|--------|
| `Connection refused` | Blender no está escuchando, el `host`/`port` no coincide, o el cliente intenta llegar a un `localhost` distinto |
| El MCP arranca pero no ve Blender | Revisar `BLENDER_HOST`, `BLENDER_PORT` y el estado del add-on en Blender |
| Cursor / Claude cargan otro `blender-mcp` | Verificar la procedencia del ejecutable; evitar asumir que cualquier paquete con ese nombre es el oficial |
| Blender en Windows, cliente en WSL | No usar `localhost` en ambos lados; exponer el add-on y apuntar al host Windows desde WSL |
| Cambiaste la config y nada pasó | Reiniciar Blender y recargar la ventana de Cursor / Claude Code |

## Requisitos

- Blender 5.1+ con el add-on / extensión oficial de Blender Lab
- Cliente MCP compatible con `.mcpb` o con procesos `stdio`
- Instalación local conocida del servidor oficial si se usa la ruta stdio

## Dónde está configurado

| Herramienta | Alcance | Archivo / artefacto |
|-------------|---------|---------------------|
| **Cursor IDE** | Global o por proyecto | `~/.cursor/mcp.json` o importación del `.mcpb` |
| **Claude Code VSCode** | Por proyecto | `~/.claude.json` -> `projects[...]` |
| **Blender** | Sesión / usuario | preferencias del add-on oficial |
| **Release oficial** | Usuario | `blender-<version>.mcpb` o `mcp-<version>.zip` |

# Guía: MCP de Obsidian

## ¿Qué hace el servidor Obsidian?

Permite a Claude leer y escribir notas en un vault de Obsidian local.  
El vault puede contener symlinks a directorios externos (como la memoria de Claude Code), que el servidor resuelve correctamente.

## Tools disponibles

| Tool           | Descripción                                                   |
|----------------|---------------------------------------------------------------|
| `get_context`  | Lee `CONTEXT.md` del vault: convenciones y estructura         |
| `read_note`    | Lee el contenido de una nota (path relativo al vault)         |
| `write_note`   | Crea o reemplaza una nota completa                            |
| `append_note`  | Agrega contenido al final de una nota existente               |
| `delete_note`  | Elimina una nota o carpeta entera (recursivo)                 |
| `search_notes` | Busca notas por contenido (grep recursivo, sigue symlinks)    |
| `list_notes`   | Lista archivos `.md` de una carpeta del vault (recursivo)     |

## Argumentos del servidor

| Argumento       | Requerido | Descripción                           |
|-----------------|-----------|---------------------------------------|
| `--vault-path`  | Sí        | Ruta absoluta al directorio del vault |

---

## Instalación paso a paso

### 1. Instalar Obsidian

**WSL2 / Linux** — WSL2 con Windows 11 soporta apps GUI nativas via WSLg:

```bash
# Descargar AppImage (verificar versión actual en https://obsidian.md/download)
wget -O ~/Obsidian.AppImage \
  "https://github.com/obsidianmd/obsidian-releases/releases/download/v1.8.10/Obsidian-1.8.10.AppImage"

chmod +x ~/Obsidian.AppImage

# Si la AppImage no abre: instalar libfuse2
sudo apt install libfuse2

# Alias para abrir fácilmente
echo 'alias obsidian="~/Obsidian.AppImage --no-sandbox &"' >> ~/.bashrc
source ~/.bashrc
```

**Windows nativo o macOS:** descargar el instalador desde [obsidian.md/download](https://obsidian.md/download).

### 2. Crear el vault

```bash
# Carpeta raíz del vault
mkdir -p ~/ObsidianVault/claude-memory
mkdir -p ~/ObsidianVault/templates

# Symlinks a la memoria de Claude por proyecto
ln -sfn ~/.claude/projects/-home-TU_USUARIO-mi-proyecto/memory \
        ~/ObsidianVault/claude-memory/mi-proyecto
```

Para listar los proyectos que tienen directorio de memoria:

```bash
for d in ~/.claude/projects/*/memory; do [ -d "$d" ] && echo "$d"; done
```

### 3. Abrir Obsidian y configurar el vault

1. Ejecutar `obsidian` (o abrir la app)
2. En la pantalla de bienvenida: **Open folder as vault** → seleccionar `~/ObsidianVault`
3. Ir a **Settings → Files & Links** → activar **"Follow symlinks outside the vault"**  
   *(sin esto Obsidian no muestra los archivos dentro de los symlinks)*

### 4. Copiar el servidor MCP

```bash
mkdir -p ~/.claude/mcp-servers/obsidian
cp "~/Mrgenkko Skills/deployed/obsidian/server.py" \
   ~/.claude/mcp-servers/obsidian/server.py
```

### 5. Registrar en `scripts/secrets.json`

```json
{
  "name": "obsidian",
  "type": "obsidian",
  "vault_path": "/home/TU_USUARIO/ObsidianVault"
}
```

### 6. Registrar el MCP en los proyectos

```bash
# Ver proyectos registrados y sus MCPs actuales
python3 "~/Mrgenkko Skills/scripts/add-mcp-to-project.py"

# Registrar en un proyecto concreto
python3 "~/Mrgenkko Skills/scripts/add-mcp-to-project.py" /ruta/absoluta/al/proyecto
```

Después: **reiniciar Claude Code en VSCode** para que cargue el MCP.

### 7. Verificar que funciona

En una sesión de Claude Code con el proyecto registrado:

```
Llama get_context y luego list_notes
```

`get_context` debe devolver el contenido de `CONTEXT.md`. `list_notes` debe listar todos los `.md` del vault incluyendo los que están dentro de los symlinks.

---

## Estructura recomendada del vault

No hay una estructura obligatoria — el servidor opera sobre cualquier carpeta. La siguiente es una referencia para proyectos con múltiples equipos o empresas:

```
ObsidianVault/
├── CONTEXT.md              ← convenciones del vault (lo lee get_context)
├── templates/
│   └── adr.md              ← plantilla ADR reutilizable
├── claude-memory/          ← symlinks a ~/.claude/projects/*/memory/
│   └── mi-proyecto/        ← memoria automática de Claude (no editar)
├── ecosistema/             ← infra y decisiones transversales
│   ├── infraestructura.md
│   ├── comunicacion.md
│   └── decisiones/
├── empresa-a/
│   ├── index.md            ← portal: links a todos los proyectos
│   └── proyectos/
│       └── servicio-x/
│           ├── index.md    ← hub del proyecto (conecta todo en el grafo)
│           ├── arquitectura.md
│           └── decisiones/
│               └── 001-mi-decision.md
└── empresa-b/
    ├── index.md
    └── proyectos/
```

### Claves de la estructura

**`CONTEXT.md`** — Claude lo lee via `get_context` antes de escribir cualquier nota. Define dónde va cada tipo de contenido. Es el nodo raíz del grafo de Obsidian: debe tener `[[links]]` a todos los índices principales.

**`templates/`** — Plantillas reutilizables. Una sola por tipo (ej: `adr.md`), no una por proyecto. Evita nodos flotantes en el grafo.

**`<empresa>/index.md`** — Lista todos los proyectos de esa empresa con `[[wikilinks]]`. Conecta el grafo verticalmente.

**`<proyecto>/index.md`** — Hub del proyecto: vincula `arquitectura.md` y todas las notas de `decisiones/`. Sin este archivo, las decisiones aparecen como nodos sueltos en el grafo.

**`claude-memory/`** — Symlinks a la memoria real de Claude. Obsidian los muestra como carpetas normales. No editar manualmente.

---

## Registro manual en `~/.claude.json`

Si no se usa `add-mcp-to-project.py`:

```json
{
  "projects": {
    "/ruta/al/proyecto": {
      "mcpServers": {
        "obsidian": {
          "type": "stdio",
          "command": "/home/TU_USUARIO/Mrgenkko Skills/.venv/bin/python",
          "args": [
            "/home/TU_USUARIO/.claude/mcp-servers/obsidian/server.py",
            "--vault-path=/home/TU_USUARIO/ObsidianVault"
          ],
          "env": {}
        }
      }
    }
  }
}
```

---

## Troubleshooting

**Obsidian no abre en WSL2**  
→ Instalar `libfuse2`: `sudo apt install libfuse2`  
→ Si `apt` falla con *"dpkg was interrupted"*, reparar primero: `sudo dpkg --configure -a`  
→ Si persiste: `sudo apt --fix-broken install`  
→ Verificar que WSLg está activo: `echo $DISPLAY` debe devolver algo como `:0`  
→ En Windows 10: requiere build 22000+; actualizar o instalar wslg: `sudo apt install wslg`

**Los symlinks aparecen vacíos en Obsidian**  
→ Activar **Settings → Files & Links → Follow symlinks outside the vault**  
→ Reiniciar Obsidian después de activar la opción

**`list_notes` devuelve `(sin notas)` cuando hay archivos**  
→ Verificar que `--vault-path` apunta al directorio correcto  
→ Confirmar que los symlinks existen: `ls ~/ObsidianVault/claude-memory/`

**El MCP no aparece en VSCode**  
→ Los MCPs deben estar en `~/.claude.json`, no en `~/.claude/settings.json`  
→ Reiniciar la extensión de Claude Code

**`Error: [Errno 2] No such file or directory` al leer una nota**  
→ El path debe ser relativo al vault, no absoluto (ej: `claude-memory/mi-proyecto/nota.md`)  
→ El servidor agrega `.md` automáticamente si el path no tiene extensión

---

## Agregar más tools al servidor

Editar `deployed/obsidian/server.py` y sincronizar con `~/.claude/mcp-servers/obsidian/server.py`.

### Ejemplo: tool para renombrar una nota

**En `list_tools()`:**

```python
types.Tool(
    name="rename_note",
    description="Renombra o mueve una nota dentro del vault.",
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path actual (relativo al vault)"},
            "new_path": {"type": "string", "description": "Nuevo path (relativo al vault)"},
        },
        "required": ["path", "new_path"],
    },
),
```

**En `call_tool()`:**

```python
elif name == "rename_note":
    src = _ensure_md(_resolve(arguments["path"]))
    dst = _ensure_md(_resolve(arguments["new_path"]))
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    output = f"Nota movida: {arguments['path']} → {arguments['new_path']}"
```

### Ejemplo: tool para eliminar una nota o carpeta (ya incluida en el servidor)

**En `list_tools()`:**

```python
types.Tool(
    name="delete_note",
    description="Elimina una nota o carpeta entera del vault (recursivo).",
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relativo al vault"},
        },
        "required": ["path"],
    },
),
```

**En `call_tool()`:**

```python
elif name == "delete_note":
    import shutil
    target = _resolve(arguments["path"])
    target.relative_to(VAULT)  # seguridad: evita salir del vault
    if not target.exists():
        target_md = target.with_suffix(".md")
        if target_md.exists():
            target = target_md
        else:
            raise FileNotFoundError(f"No existe: {arguments['path']}")
    if target.is_dir():
        shutil.rmtree(target)
        output = f"Carpeta eliminada: {arguments['path']}"
    else:
        target.unlink()
        output = f"Nota eliminada: {arguments['path']}"
```

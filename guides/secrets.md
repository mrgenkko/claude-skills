# Credenciales de los MCP: el llavero del sistema

Las contraseñas y los tokens de los MCP **no viven en ningún archivo**: viven en el llavero del
sistema operativo, que es GNOME Keyring en Ubuntu y el Administrador de credenciales en Windows.
`scripts/secrets.json` guarda la configuración y, en cada campo secreto, solo una **referencia**:

```json
{ "name": "postgres-gz-prod", "type": "postgres", "host": "...", "password": "keyring:postgres-gz-prod/password" }
```

Lo que se consigue es que ninguna clave aparezca con un `cat`, un `grep` o un `ps aux`. Tampoco
queda en `~/.claude.json`, en los `.codex/config.toml`, en los backups, en los transcripts ni en un
commit por error.

## Cómo llega la clave al MCP

```
secrets.json  ──(solo "keyring:<id>")──►  add-mcp-to-project.py
                                              │ registra SIN valores
                                              ▼
  ~/.claude.json / .codex/config.toml:
    python mcp-launch.py --secret MCP_PASSWORD=keyring:postgres-gz-prod/password -- server.py --db=...
                                              │ al arrancar el MCP
                                              ▼
  mcp-launch.py ── lee el llavero ──► pone el valor en el entorno ──► os.execv(server.py)
```

- **MCP locales (stdio):** `scripts/mcp-launch.py` lee las claves, las pone en el entorno y se
  reemplaza a sí mismo por el server (`execv`: mismo PID). En `ps` solo queda `python server.py`,
  sin claves ni ids.
- **MCP remotos (focusyn):** Claude Code (`headersHelper`) y Codex (`http_headers_helper`) corren
  `~/.claude/mcp-servers/_launch/headers-<nombre>` en cada conexión. Ese ejecutable imprime los
  headers y contiene solo ids, nunca valores. Su ruta no tiene espacios ni argumentos a propósito,
  porque Codex no documenta si pasa ese string por un shell.

Cada server lee su variable:

| Tipo | Campo en `secrets.json` | Variable |
|---|---|---|
| postgres, redis | `password` | `MCP_PASSWORD` |
| ssh | `password`, `sudo_password` | `MCP_PASSWORD`, `MCP_SUDO_PASSWORD` |
| gh | `token` | `GH_TOKEN` |
| modal | `token_id`, `token_secret` | `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` |
| focusyn | `agent_key` | header `X-Agent-Key` |

gcloud no figura en la tabla: `key_file` es la **ruta** a un JSON de service account, y gcloud
necesita el archivo. Esos JSON viven en `~/keys/`, con permisos `700` en la carpeta y `600` en los
archivos.

## `scripts/mcp-secrets.py`

Hay que correrlo con el Python del venv, porque necesita `keyring`:

```bash
PY="$HOME/Mrgenkko Skills/.venv/bin/python"
"$PY" scripts/mcp-secrets.py set postgres-gz-prod/password   # guarda o reemplaza; la pide sin mostrarla
"$PY" scripts/mcp-secrets.py check                           # ¿cada keyring:<id> de secrets.json existe?
"$PY" scripts/mcp-secrets.py list                            # ids en el llavero; marca los huérfanos
"$PY" scripts/mcp-secrets.py delete vieja/clave              # borra (avisa si un MCP la usa)
"$PY" scripts/mcp-secrets.py migrate --dry-run               # qué valores en texto plano pasaría
"$PY" scripts/mcp-secrets.py migrate                         # los pasa, verifica y reescribe secrets.json
"$PY" scripts/mcp-secrets.py migrate --finalize              # borra el secrets.json.bak que deja migrate
```

Nunca imprime valores. No se llama `secrets.py` porque ese nombre taparía el módulo `secrets` de
la stdlib para todo lo que corre desde `scripts/`.

También se ven desde la interfaz: **Contraseñas y claves → Inicio de sesión**, servicio `mrgenkko-mcp`.

## Tareas habituales

**Un MCP nuevo con clave:**
1. Agregar la entrada en `secrets.json` con `"password": "keyring:<nombre>/password"` (o el campo
   que corresponda según la tabla).
2. `mcp-secrets.py set <nombre>/password`
3. `add-mcp-to-project.py /ruta/al/proyecto --only <nombre>`

**Rotar una clave:** `mcp-secrets.py set <id>` y reconectar el MCP (`/mcp` o recargar VSCode).
**No hace falta volver a registrar nada**, porque el registro solo tiene el nombre de la clave.

**Varias instancias con la misma clave:** se puede apuntar dos entradas al mismo id
(`"keyring:pg-gz/password"`), y así una sola rotación alcanza para las dos. `migrate` crea una
entrada por instancia; compartir una clave es una decisión que se toma a mano.

**Re-registrar todo** (después de cambiar el formato de registro, o para limpiar):
```bash
"$PY" scripts/add-mcp-to-project.py --sweep --dry-run   # muestra qué cambia y avisa de todo lo que no sean credenciales
"$PY" scripts/add-mcp-to-project.py --sweep
```
`--sweep` solo actualiza los MCPs que ya están en cada scope (Claude y Codex): no agrega ninguno y
no toca los que no salen de `secrets.json`.
Valida todo antes de escribir (configs trackeados en git, marcadores rotos): si algo falla, no
escribe nada. Si encuentra cambios que **no** son de credenciales, los lista y se frena: hay que
volver a correrlo con `--yes`. No toca el trust de Codex ni los `.gitignore`, y regenera los helpers
de headers que falten o hayan quedado desactualizados (por ejemplo, si se movió el repo).

## Cuando falla

Los mensajes salen del lanzador por stderr; en Claude Code se ven en `/mcp`.

| Mensaje | Causa | Qué hacer |
|---|---|---|
| «no hay llavero disponible… estas claves solo se leen desde la sesión gráfica» | No hay bus D-Bus de sesión: SSH, cron o un servicio | Usar los MCP desde la sesión de escritorio. **No hay respaldo en texto plano, a propósito** |
| «el llavero está bloqueado o se canceló el desbloqueo» | Se canceló la ventana de desbloqueo | Desbloquear «Inicio de sesión» en Contraseñas y claves y reconectar |
| «nadie desbloqueó el llavero en 25 s» | Llavero bloqueado y nadie frente a la pantalla | Lo mismo. Sin este tope, `keyring` esperaría la ventana **para siempre** |
| «keyring:<id> no existe» | Falta la clave | `mcp-secrets.py set <id>` |

Si el llavero está bloqueado y arrancan varios MCPs a la vez, sale **una sola** ventana de
desbloqueo: el lanzador serializa las lecturas con un lock en `$XDG_RUNTIME_DIR`.

## Lo que esto NO protege

- **Un programa que corre con tu usuario** puede pedirle cualquier clave al llavero mientras la
  sesión está abierta (`secret-tool lookup ...`). En Windows pasa lo mismo. El objetivo es que las
  claves no queden visibles por descuido, no protegerlas de malware.
- **`/proc/<pid>/environ`** de un MCP vivo sigue mostrando su clave, aunque el server la borre de
  `os.environ` al leerla. Ese archivo refleja el entorno con el que arrancó el proceso. Solo lo lee
  el mismo usuario (permisos `0400`); `ps` y los demás usuarios no la ven. Para cerrarlo del todo
  habría que pasar la clave por un pipe en vez de por el entorno.
- **Los MCP registrados a mano** (por ejemplo, uno http con un `Authorization` fijo) no pasan por
  `secrets.json`, así que su clave sigue en texto plano en `~/.claude.json`.

## Detalles de plataforma

- **Linux:** backend `SecretService` (paquetes `keyring`, `jeepney` y `secretstorage`). Los MCP
  lanzados por VSCode heredan `DBUS_SESSION_BUS_ADDRESS`; en Codex, el registro agrega
  `env_vars = ["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"]` porque Codex filtra el entorno de
  sus MCP.
- **Windows:** WinVault, sin ventana de desbloqueo y sin lock. `execv` no conserva el proceso, así
  que el lanzador corre el server como hijo con la E/S heredada. El límite es de unos 2,5 KB por
  credencial: alcanza para claves y tokens, pero no para los JSON de service account.

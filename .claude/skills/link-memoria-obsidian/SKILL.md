---
name: link-memoria-obsidian
description: Vincula la carpeta de claude-memory física de un proyecto con el vault de Obsidian creando un symlink en ~/ObsidianVault/claude-memory/. Uso: /link-memoria-obsidian <nombre-proyecto>
argument-hint: <nombre-proyecto>
allowed-tools: Bash
---

# link-memoria-obsidian · Vincular claude-memory con Obsidian

Crea un symlink en `~/ObsidianVault/claude-memory/<nombre>` apuntando a
`~/.claude/projects/<slug>/memory`, para que la memoria física del proyecto
sea visible desde Obsidian.

## Argumentos

`$ARGUMENTS` es el nombre del proyecto (parcial o exacto):

| Ejemplo | Resultado |
|---|---|
| `lait-aioperation-discovery` | Vincula ese proyecto exacto |
| `aioperation` | Busca el proyecto que contenga ese string |

## Flujo de ejecución

1. Buscar en `~/.claude/projects/` el directorio cuyo nombre contenga `$ARGUMENTS` (case-insensitive, reemplazando guiones por el patrón slug `*<término>*`).
2. Si hay más de un match, listarlos y pedir al usuario que elija.
3. Si no hay ningún match, reportar el error y detenerse.
4. Verificar que exista la subcarpeta `memory/` dentro del directorio encontrado. Si no existe, reportar que el proyecto no tiene memoria aún y detenerse.
5. Determinar el nombre del symlink: tomar la parte final del slug del directorio (ej. `-home-melquiades-lait-aioperation-discovery` → `lait-aioperation-discovery`).
6. Verificar que no exista ya un symlink o carpeta con ese nombre en `~/ObsidianVault/claude-memory/`. Si existe, reportarlo y detenerse.
7. Crear el symlink:
   ```bash
   ln -s ~/.claude/projects/<slug>/memory ~/ObsidianVault/claude-memory/<nombre>
   ```
8. Verificar que el symlink funciona (`ls` sobre él) y listar los archivos de memoria encontrados.
9. Reportar: nombre del symlink creado, ruta física, cantidad de archivos de memoria.

## Comandos de referencia

```bash
# Buscar el slug del proyecto
ls ~/.claude/projects/ | grep -i "<término>"

# Extraer el nombre desde el slug (quitar el prefijo -home-melquiades-)
echo "-home-melquiades-lait-aioperation-discovery" | sed 's/^-home-melquiades-//'

# Crear el symlink
ln -s ~/.claude/projects/<slug>/memory ~/ObsidianVault/claude-memory/<nombre>

# Verificar
ls ~/ObsidianVault/claude-memory/<nombre>/
```

## Casos de error

| Situación | Acción |
|---|---|
| Ningún proyecto coincide | Reportar y sugerir `ls ~/.claude/projects/` para ver los disponibles |
| Más de un proyecto coincide | Listar los matches y pedir al usuario que especifique |
| No existe `memory/` en el proyecto | Informar que el proyecto aún no tiene archivos de memoria generados |
| El symlink ya existe en Obsidian | Informar que ya está vinculado y mostrar su destino actual |

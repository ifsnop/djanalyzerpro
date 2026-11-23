#!/usr/bin/env bash
#
# tag_mp3.sh — Escribe en el MP3 la tracklist detectada por djanalyzerpro.
#
#   - Capítulos ID3v2 (CHAP + CTOC): un capítulo por tema, "Artista - Título".
#   - Comentario (COMM) con la tracklist en texto.
#   - genre=DJ Mix y album_artist=Various Artists (si no existe).
#   - Genera además un .cue junto al MP3.
#
# Conserva los tags y la carátula existentes. No recodifica el audio.
#
# Requisitos: ffmpeg, ffprobe, jq
# Recomendado: mid3v2 (paquete python3-mutagen) para escribir el COMM.
#
# Uso:
#   ./tag_mp3.sh sesion.mp3 [results_dj/analisis_completo.json] [--no-backup] [--force]
#
#   --no-backup  No crear sesion.mp3.bak antes de modificar.
#   --force      Etiquetar aunque la duración del MP3 no coincida con la del JSON.

set -euo pipefail

TOLERANCIA_DURACION=2   # segundos

uso() {
    sed -n '15,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
}

error() {
    echo "ERROR: $*" >&2
    exit 1
}

# ------------------------------------------------------------
# Argumentos
# ------------------------------------------------------------

mp3=""
json=""
backup=1
force=0

for arg in "$@"; do
    case "$arg" in
        --no-backup) backup=0 ;;
        --force)     force=1 ;;
        -h|--help)   uso ;;
        -*)          error "Opción desconocida: $arg" ;;
        *)
            if [[ -z "$mp3" ]]; then
                mp3="$arg"
            elif [[ -z "$json" ]]; then
                json="$arg"
            else
                uso
            fi
            ;;
    esac
done

[[ -n "$mp3" ]] || uso
json="${json:-results_dj/analisis_completo.json}"

for programa in ffmpeg ffprobe jq; do
    command -v "$programa" >/dev/null 2>&1 \
        || error "No se encuentra '$programa'. Instálalo (sudo apt install ffmpeg jq)."
done

[[ -f "$mp3" ]]  || error "No existe el MP3: $mp3"
[[ -f "$json" ]] || error "No existe el JSON: $json"

jq -e '.tracklist | type == "array"' "$json" >/dev/null 2>&1 \
    || error "El JSON no tiene el formato de djanalyzerpro (falta 'tracklist')."

# ------------------------------------------------------------
# Salvaguarda: el JSON corresponde a este MP3
# ------------------------------------------------------------

dur_mp3=$(ffprobe -v error -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$mp3")
dur_json=$(jq -r '.duracion_segundos' "$json")

if ! awk -v a="$dur_mp3" -v b="$dur_json" -v t="$TOLERANCIA_DURACION" \
        'BEGIN { d = a - b; if (d < 0) d = -d; exit !(d <= t) }'; then
    if (( force )); then
        echo "AVISO: la duración del MP3 (${dur_mp3}s) no coincide con la del JSON (${dur_json}s). Continuando por --force."
    else
        error "La duración del MP3 (${dur_mp3}s) no coincide con la del JSON (${dur_json}s).
       ¿Es el análisis de otro fichero? Usa --force para etiquetar igualmente."
    fi
fi

num_temas=$(jq '.tracklist | length' "$json")
if (( num_temas == 0 )); then
    echo "La tracklist está vacía: no hay nada que etiquetar."
    exit 0
fi

# ------------------------------------------------------------
# Ficheros temporales
# ------------------------------------------------------------

tmpdir=$(mktemp -d)
dir_mp3=$(dirname -- "$mp3")
tmp_out="$dir_mp3/.$(basename -- "$mp3").tagging.$$"

limpiar() {
    rm -rf -- "$tmpdir"
    rm -f -- "$tmp_out"
}
trap limpiar EXIT

meta="$tmpdir/chapters.ffmeta"

# Funciones jq compartidas.
JQ_LIB='
def hms:
    (. | floor) as $s
    | [($s / 3600 | floor), ($s % 3600 / 60 | floor), ($s % 60)]
    | map(tostring | if length < 2 then "0" + . else . end)
    | join(":");
def nombre: "\(.artista) - \(.titulo)";
def fin($dur): (.fin_estimado // $dur);
'

# ------------------------------------------------------------
# Capítulos (formato ffmetadata)
# ------------------------------------------------------------

jq -r "$JQ_LIB"'
def esc: gsub("(?<c>[=;#\\\\\n])"; "\\\(.c)");
.duracion_segundos as $dur
| ";FFMETADATA1",
  ( .tracklist[]
    | ((.inicio * 1000) | round) as $ini
    | ((fin($dur) * 1000) | round) as $fin
    | "",
      "[CHAPTER]",
      "TIMEBASE=1/1000",
      "START=\($ini)",
      "END=\(if $fin > $ini then $fin else $ini + 1 end)",
      "title=\(nombre | esc)"
  )
' "$json" > "$meta"

# ------------------------------------------------------------
# Comentario con la tracklist
# ------------------------------------------------------------

comentario=$(jq -r "$JQ_LIB"'
.tracklist
| to_entries[]
| "\(.key + 1 | tostring | if length < 3 then ("00" + .)[-3:] else . end). \(.value.inicio | hms) \(.value | nombre)"
' "$json")

# ------------------------------------------------------------
# Escribir tags
# ------------------------------------------------------------

extra_meta=()
album_artist=$(ffprobe -v error -show_entries format_tags=album_artist \
    -of default=noprint_wrappers=1:nokey=1 "$mp3" || true)
if [[ -z "$album_artist" ]]; then
    extra_meta+=(-metadata "album_artist=Various Artists")
fi

# ffmpeg guarda "comment" como TXXX:comment, que casi ningún reproductor
# muestra. Si está mid3v2 (mutagen), se escribe un COMM real; si no,
# se usa TXXX como alternativa.
if command -v mid3v2 >/dev/null 2>&1; then
    usar_mid3v2=1
    extra_meta+=(-metadata "comment=")   # vacío: elimina el TXXX:comment
else
    usar_mid3v2=0
    extra_meta+=(-metadata "comment=$comentario")
    echo "AVISO: no se encuentra mid3v2; la tracklist se guarda como TXXX:comment."
    echo "       Para un COMM estándar instala mutagen (sudo apt install python3-mutagen)."
fi

ffmpeg -hide_banner -loglevel error -y \
    -i "$mp3" \
    -f ffmetadata -i "$meta" \
    -map 0 -map_metadata 0 -map_chapters 1 \
    -c copy \
    -id3v2_version 4 \
    -metadata "genre=DJ Mix" \
    "${extra_meta[@]}" \
    -f mp3 "$tmp_out"

if (( usar_mid3v2 )); then
    # Formato "descripción:texto:idioma": mid3v2 vuelve a unir los ':'
    # intermedios, así que el texto no necesita escaparse.
    mid3v2 --COMM ":$comentario:eng" "$tmp_out" >/dev/null
fi

if (( backup )); then
    if [[ -e "$mp3.bak" ]]; then
        echo "Ya existe $mp3.bak: se conserva (no se sobrescribe)."
    else
        cp -p -- "$mp3" "$mp3.bak"
        echo "Copia de seguridad: $mp3.bak"
    fi
fi

mv -f -- "$tmp_out" "$mp3"

# ------------------------------------------------------------
# CUE sheet
# ------------------------------------------------------------

cue="${mp3%.*}.cue"

jq -r --arg fichero "$(basename -- "$mp3")" '
def q: gsub("\""; "'"'"'");
def pad2: tostring | if length < 2 then "0" + . else . end;
def cuetime:
    ((. * 75) | round) as $f
    | "\($f / 75 / 60 | floor | pad2):\($f / 75 | floor % 60 | pad2):\($f % 75 | pad2)";
"TITLE \"\($fichero | sub("\\.[^.]*$"; "") | q)\"",
"FILE \"\($fichero | q)\" MP3",
( .tracklist
  | to_entries[]
  | "  TRACK \(.key + 1 | pad2) AUDIO",
    "    PERFORMER \"\(.value.artista | q)\"",
    "    TITLE \"\(.value.titulo | q)\"",
    "    INDEX 01 \(.value.inicio | cuetime)"
)
' "$json" > "$cue"

# ------------------------------------------------------------
# Resumen
# ------------------------------------------------------------

echo "MP3 etiquetado: $mp3 ($num_temas capítulos)"
echo "CUE generado:   $cue"

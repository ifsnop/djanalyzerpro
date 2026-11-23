
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DJ ANALYZER PRO
Analiza sesiones DJ con FFmpeg y ShazamIO.

Requisitos:
    Python 3.11+
    ffmpeg
    ffprobe
    pip install shazamio numpy

Uso:
    python dj_analyzer.py "/ruta/a/sesion.mp3"
"""

import asyncio
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time

from datetime import datetime
from pathlib import Path

import numpy as np
from shazamio import Shazam


# ============================================================
# CONFIGURACIÓN
# ============================================================

CARPETA_SALIDA = Path("results_dj")

# Muestreo periódico: una consulta cada 45 segundos.
INTERVALO_BASE = 45

# Duración del fragmento enviado a Shazam.
DURACION_CLIP = 12

# No permitir muestras excesivamente próximas.
SEPARACION_MINIMA = 25

# Análisis acústico.
VENTANA_ANALISIS = 2
PASO_ANALISIS = 1

# Sensibilidad del detector.
UMBRAL_RMS_DB = 5.0
UMBRAL_CENTROIDE = 0.28
UMBRAL_BANDAS = 0.22

# Concurrencia y pausas para no saturar el servicio.
CONCURRENCIA = 2
PAUSA_ENTRE_CONSULTAS = 2.5
MAX_REINTENTOS = 2
TIMEOUT_SHazam = 90

# Audio de análisis.
FRECUENCIA_ANALISIS = 8000
FRECUENCIA_CLIP = 16000

# Logging.
CARPETA_SALIDA.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            CARPETA_SALIDA / "analisis_verbose.log",
            encoding="utf-8"
        )
    ]
)

log = logging.getLogger("DJ_ANALYZER")


# ============================================================
# UTILIDADES
# ============================================================

def comprobar_dependencias():
    for programa in ("ffmpeg", "ffprobe"):
        if shutil.which(programa) is None:
            raise RuntimeError(
                f"No se encuentra {programa}. "
                "Instálalo con: sudo apt install ffmpeg"
            )


def comprobar_archivo(ruta):
    archivo = Path(ruta).expanduser().resolve()

    if not archivo.exists():
        raise FileNotFoundError(f"No existe el archivo: {archivo}")

    if not archivo.is_file():
        raise ValueError("La ruta indicada no es un archivo.")

    return archivo


def obtener_duracion(archivo):
    comando = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(archivo)
    ]

    resultado = subprocess.run(
        comando,
        capture_output=True,
        text=True,
        check=True
    )

    return float(resultado.stdout.strip())


def tiempo_legible(segundos):
    segundos = max(0, int(segundos))
    horas = segundos // 3600
    minutos = (segundos % 3600) // 60
    segundos = segundos % 60

    return f"{horas:02d}:{minutos:02d}:{segundos:02d}"


def limpiar_nombre(texto):
    return re.sub(r'[\\/*?:"<>|]', "_", str(texto))


# ============================================================
# DECODIFICACIÓN PARA ANÁLISIS ACÚSTICO
# ============================================================

def decodificar_audio(archivo):
    log.info("Decodificando audio para analizar transiciones...")

    comando = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(archivo),
        "-vn",
        "-ac", "1",
        "-ar", str(FRECUENCIA_ANALISIS),
        "-f", "s16le",
        "-"
    ]

    proceso = subprocess.run(
        comando,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    if proceso.returncode != 0:
        raise RuntimeError(
            "Error de FFmpeg: "
            + proceso.stderr.decode(errors="replace")
        )

    audio = np.frombuffer(proceso.stdout, dtype=np.int16)
    audio = audio.astype(np.float32) / 32768.0

    if len(audio) == 0:
        raise RuntimeError("FFmpeg no ha podido decodificar el audio.")

    log.info(
        "Audio decodificado: %.1f minutos",
        len(audio) / FRECUENCIA_ANALISIS / 60
    )

    return audio


# ============================================================
# ANÁLISIS DE CARACTERÍSTICAS DEL SONIDO
# ============================================================

def extraer_caracteristicas(fragmento):
    """
    Calcula RMS, centroide espectral y proporción de energía
    en bandas de frecuencia.
    """

    fragmento = fragmento.astype(np.float32)

    if len(fragmento) == 0:
        return None

    rms = float(np.sqrt(np.mean(fragmento ** 2)) + 1e-10)
    rms_db = 20 * np.log10(rms)

    ventana = np.hanning(len(fragmento))
    espectro = np.abs(np.fft.rfft(fragmento * ventana))

    frecuencias = np.fft.rfftfreq(
        len(fragmento),
        d=1 / FRECUENCIA_ANALISIS
    )

    energia_total = float(np.sum(espectro ** 2)) + 1e-10

    centroide = float(
        np.sum(frecuencias * espectro) /
        (np.sum(espectro) + 1e-10)
    )

    energia_baja = np.sum(espectro[frecuencias < 250] ** 2)
    energia_media = np.sum(
        espectro[
            (frecuencias >= 250) &
            (frecuencias < 2500)
        ] ** 2
    )
    energia_alta = np.sum(espectro[frecuencias >= 2500] ** 2)

    bandas = np.array([
        energia_baja,
        energia_media,
        energia_alta
    ], dtype=np.float32) / energia_total

    return {
        "rms_db": float(rms_db),
        "centroide": centroide,
        "bandas": bandas
    }


def analizar_transiciones(audio):
    """
    Busca cambios acústicos sostenidos.

    No interpreta cada cambio de batería como transición:
    exige cambios importantes en varias características
    y persistencia en ventanas consecutivas.
    """

    log.info("Buscando posibles transiciones...")

    ventana = int(VENTANA_ANALISIS * FRECUENCIA_ANALISIS)
    paso = int(PASO_ANALISIS * FRECUENCIA_ANALISIS)

    caracteristicas = []
    tiempos = []

    for inicio in range(0, len(audio) - ventana, paso):
        fragmento = audio[inicio:inicio + ventana]
        datos = extraer_caracteristicas(fragmento)

        if datos is not None:
            caracteristicas.append(datos)
            tiempos.append(inicio / FRECUENCIA_ANALISIS)

    candidatos = []

    for i in range(1, len(caracteristicas)):
        anterior = caracteristicas[i - 1]
        actual = caracteristicas[i]

        cambio_rms = abs(
            actual["rms_db"] - anterior["rms_db"]
        )

        cambio_centroide = abs(
            actual["centroide"] - anterior["centroide"]
        ) / max(anterior["centroide"], 100)

        cambio_bandas = float(
            np.mean(
                np.abs(actual["bandas"] - anterior["bandas"])
            )
        )

        cambios = [
            cambio_rms >= UMBRAL_RMS_DB,
            cambio_centroide >= UMBRAL_CENTROIDE,
            cambio_bandas >= UMBRAL_BANDAS
        ]

        # Exigimos al menos dos características alteradas.
        if sum(cambios) >= 2:
            candidatos.append(tiempos[i])

    # Exigir persistencia: agrupar cambios que aparecen
    # en ventanas cercanas y conservar un único candidato.
    persistentes = []

    for t in candidatos:
        cercanos = [
            x for x in candidatos
            if abs(x - t) <= 2
        ]

        if len(cercanos) >= 2:
            persistentes.append(t)

    # Agrupar candidatos cercanos.
    agrupados = []

    for t in persistentes:
        if not agrupados or t - agrupados[-1][-1] > 8:
            agrupados.append([t])
        else:
            agrupados[-1].append(t)

    transiciones = [
        float(np.median(grupo))
        for grupo in agrupados
    ]

    # Separación mínima entre transiciones.
    filtradas = []

    for t in transiciones:
        if (
            not filtradas
            or t - filtradas[-1] >= SEPARACION_MINIMA
        ):
            filtradas.append(t)

    log.info(
        "Transiciones candidatas: %d",
        len(filtradas)
    )

    for t in filtradas:
        log.info(
            "Posible transición acústica en %s",
            tiempo_legible(t)
        )

    return filtradas


# ============================================================
# CONSTRUCCIÓN DE MUESTRAS
# ============================================================

def construir_muestras(duracion, transiciones):
    """
    Combina muestreo periódico y muestras próximas
    a posibles transiciones.
    """

    muestras = []

    # Muestreo periódico.
    t = 5

    while t + DURACION_CLIP <= duracion:
        muestras.append({
            "segundo": float(t),
            "tipo": "PERIODICA"
        })
        t += INTERVALO_BASE

    # Muestras próximas a transiciones.
    for transicion in transiciones:
        # Se toma un fragmento unos segundos después
        # del cambio para dar tiempo a que entre el nuevo tema.
        t = transicion + 5

        if t + DURACION_CLIP <= duracion:
            muestras.append({
                "segundo": float(t),
                "tipo": "TRANSICION"
            })

    muestras.sort(key=lambda x: x["segundo"])

    # Evitar consultas demasiado próximas.
    filtradas = []

    for muestra in muestras:
        if (
            not filtradas
            or muestra["segundo"] -
            filtradas[-1]["segundo"] >= SEPARACION_MINIMA
        ):
            filtradas.append(muestra)

        elif muestra["tipo"] == "TRANSICION":
            # Si hay una muestra periódica muy próxima,
            # preferimos la que está asociada a transición.
            if filtradas[-1]["tipo"] == "PERIODICA":
                filtradas[-1] = muestra

    return filtradas


# ============================================================
# EXTRACCIÓN DE CLIPS
# ============================================================

def extraer_clip_bytes(archivo, segundo):
    comando = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", str(segundo),
        "-i", str(archivo),
        "-t", str(DURACION_CLIP),
        "-vn",
        "-ac", "1",
        "-ar", str(FRECUENCIA_CLIP),
        "-b:a", "96k",
        "-f", "mp3",
        "pipe:1"
    ]

    resultado = subprocess.run(
        comando,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    if resultado.returncode != 0:
        raise RuntimeError(
            resultado.stderr.decode(errors="replace")
        )

    if not resultado.stdout:
        raise RuntimeError("El clip extraído está vacío.")

    return resultado.stdout


# ============================================================
# RECONOCIMIENTO SHAZAM
# ============================================================

class ControlConsultas:

    def __init__(self):
        self.semaforo = asyncio.Semaphore(CONCURRENCIA)
        self.bloqueo = asyncio.Lock()
        self.ultima_consulta = 0.0

    async def esperar_turno(self):
        async with self.bloqueo:
            ahora = time.monotonic()
            espera = (
                PAUSA_ENTRE_CONSULTAS
                - (ahora - self.ultima_consulta)
            )

            if espera > 0:
                log.info(
                    "Esperando %.1f segundos antes de consultar...",
                    espera
                )
                await asyncio.sleep(espera)

            self.ultima_consulta = time.monotonic()


async def reconocer_muestra(
    shazam,
    archivo,
    muestra,
    indice,
    total,
    control
):
    segundo = muestra["segundo"]
    tipo = muestra["tipo"]

    resultado = {
        "segundo": segundo,
        "tiempo": tiempo_legible(segundo),
        "tipo": tipo,
        "artista": "",
        "titulo": "",
        "coincidencias": 0,
        "estado": "NO_IDENTIFICADA",
        "error": ""
    }

    log.info(
        "[%d/%d] Preparando %s en %s",
        indice,
        total,
        tipo,
        tiempo_legible(segundo)
    )

    try:
        async with control.semaforo:

            log.info(
                "[%d/%d] Extrayendo fragmento de audio...",
                indice,
                total
            )

            audio_bytes = await asyncio.to_thread(
                extraer_clip_bytes,
                archivo,
                segundo
            )

            log.info(
                "[%d/%d] Fragmento preparado: %.1f KB",
                indice,
                total,
                len(audio_bytes) / 1024
            )

            for intento in range(1, MAX_REINTENTOS + 2):

                try:
                    await control.esperar_turno()

                    log.info(
                        "[%d/%d] Enviando a Shazam "
                        "(intento %d)...",
                        indice,
                        total,
                        intento
                    )

                    inicio = time.monotonic()

                    respuesta = await asyncio.wait_for(
                        shazam.recognize(audio_bytes),
                        timeout=TIMEOUT_SHazam
                    )

                    tiempo_respuesta = (
                        time.monotonic() - inicio
                    )

                    track = respuesta.get("track") or {}

                    if track:
                        artista = track.get(
                            "subtitle",
                            "Desconocido"
                        )

                        titulo = track.get(
                            "title",
                            "Desconocido"
                        )

                        resultado.update({
                            "artista": artista,
                            "titulo": titulo,
                            "coincidencias": len(
                                respuesta.get("matches", [])
                            ),
                            "estado": "IDENTIFICADA",
                            "respuesta_segundos": round(
                                tiempo_respuesta, 2
                            )
                        })

                        log.info(
                            "[%d/%d] IDENTIFICADA: %s - %s "
                            "(%.2f s)",
                            indice,
                            total,
                            artista,
                            titulo,
                            tiempo_respuesta
                        )

                    else:
                        log.info(
                            "[%d/%d] Shazam no ha encontrado "
                            "ninguna coincidencia.",
                            indice,
                            total
                        )

                    return resultado

                except asyncio.TimeoutError:
                    mensaje = "Tiempo de espera agotado"

                except Exception as exc:
                    mensaje = str(exc)

                resultado["error"] = mensaje

                log.warning(
                    "[%d/%d] Error: %s",
                    indice,
                    total,
                    mensaje
                )

                if intento <= MAX_REINTENTOS:
                    espera = 2 ** intento

                    log.info(
                        "Reintentando en %d segundos...",
                        espera
                    )

                    await asyncio.sleep(espera)

    except Exception as exc:
        resultado["error"] = str(exc)

        log.exception(
            "[%d/%d] Error procesando la muestra",
            indice,
            total
        )

    return resultado


# ============================================================
# GENERACIÓN DEL TRACKLIST
# ============================================================

def generar_tracklist(resultados):
    """
    Agrupa identificaciones consecutivas.

    Si suena A, después B y luego vuelve A,
    se registran dos apariciones de A.
    """

    identificaciones = [
        r for r in sorted(
            resultados,
            key=lambda x: x["segundo"]
        )
        if r["estado"] == "IDENTIFICADA"
    ]

    apariciones = []

    for resultado in identificaciones:
        clave = (
            resultado["artista"].strip().lower(),
            resultado["titulo"].strip().lower()
        )

        if (
            apariciones
            and apariciones[-1]["clave"] == clave
        ):
            apariciones[-1]["ultima_muestra"] = (
                resultado["segundo"]
            )

            apariciones[-1]["muestras"] += 1

        else:
            apariciones.append({
                "clave": clave,
                "artista": resultado["artista"],
                "titulo": resultado["titulo"],
                "inicio": resultado["segundo"],
                "ultima_muestra": resultado["segundo"],
                "muestras": 1
            })

    for i, aparicion in enumerate(apariciones):
        if i + 1 < len(apariciones):
            aparicion["fin_estimado"] = (
                apariciones[i + 1]["inicio"]
            )
        else:
            aparicion["fin_estimado"] = None

    return apariciones


# ============================================================
# EXPORTACIÓN DE RESULTADOS
# ============================================================

def exportar_resultados(resultados, tracklist, duracion):

    CARPETA_SALIDA.mkdir(exist_ok=True)

    # CSV de todas las consultas.
    with open(
        CARPETA_SALIDA / "analisis_completo.csv",
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as archivo:

        campos = [
            "segundo",
            "tiempo",
            "tipo",
            "artista",
            "titulo",
            "coincidencias",
            "estado",
            "error"
        ]

        escritor = csv.DictWriter(
            archivo,
            fieldnames=campos,
            extrasaction="ignore"
        )

        escritor.writeheader()
        escritor.writerows(resultados)

    # JSON completo.
    datos_json = {
        "fecha_analisis": datetime.now().isoformat(),
        "duracion_segundos": duracion,
        "duracion": tiempo_legible(duracion),
        "total_muestras": len(resultados),
        "total_identificadas": sum(
            1 for r in resultados
            if r["estado"] == "IDENTIFICADA"
        ),
        "tracklist": tracklist,
        "resultados": resultados
    }

    with open(
        CARPETA_SALIDA / "analisis_completo.json",
        "w",
        encoding="utf-8"
    ) as archivo:

        json.dump(
            datos_json,
            archivo,
            indent=4,
            ensure_ascii=False
        )

    # Tracklist TXT.
    with open(
        CARPETA_SALIDA / "tracklist.txt",
        "w",
        encoding="utf-8"
    ) as archivo:

        archivo.write("DJ SESSION - TRACKLIST\n")
        archivo.write("=" * 65 + "\n")
        archivo.write(
            f"Duración de sesión: {tiempo_legible(duracion)}\n"
        )
        archivo.write(
            f"Fecha de análisis: {datetime.now():%d/%m/%Y %H:%M}\n"
        )
        archivo.write("=" * 65 + "\n\n")

        for numero, track in enumerate(tracklist, 1):

            inicio = tiempo_legible(track["inicio"])

            if track["fin_estimado"] is not None:
                fin = tiempo_legible(track["fin_estimado"])
            else:
                fin = "Final de sesión / sin determinar"

            archivo.write(
                f"{numero:03d}. {track['artista']} - "
                f"{track['titulo']}\n"
            )

            archivo.write(
                f"      Entrada aprox.: {inicio}\n"
            )

            archivo.write(
                f"      Salida aprox.:  {fin}\n"
            )

            archivo.write(
                f"      Muestras reconocidas: {track['muestras']}\n\n"
            )

    log.info("Archivos exportados en: %s", CARPETA_SALIDA.resolve())


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

async def analizar_sesion(archivo):

    comprobar_dependencias()

    duracion = obtener_duracion(archivo)

    log.info("=" * 65)
    log.info("DJ ANALYZER PRO")
    log.info("Archivo: %s", archivo)
    log.info("Duración: %s", tiempo_legible(duracion))
    log.info("=" * 65)

    audio = await asyncio.to_thread(
        decodificar_audio,
        archivo
    )

    transiciones = await asyncio.to_thread(
        analizar_transiciones,
        audio
    )

    # Liberamos la memoria del audio completo.
    del audio

    muestras = construir_muestras(
        duracion,
        transiciones
    )

    log.info("Muestras totales programadas: %d", len(muestras))
    log.info("Concurrencia: %d", CONCURRENCIA)

    control = ControlConsultas()
    resultados = []

    async with Shazam() as shazam:

        tareas = [
            reconocer_muestra(
                shazam,
                archivo,
                muestra,
                indice,
                len(muestras),
                control
            )
            for indice, muestra in enumerate(muestras, 1)
        ]

        resultados = await asyncio.gather(*tareas)

    tracklist = generar_tracklist(resultados)

    exportar_resultados(
        resultados,
        tracklist,
        duracion
    )

    identificadas = sum(
        1 for r in resultados
        if r["estado"] == "IDENTIFICADA"
    )

    log.info("=" * 65)
    log.info("ANÁLISIS FINALIZADO")
    log.info("Muestras analizadas: %d", len(resultados))
    log.info("Muestras identificadas: %d", identificadas)
    log.info("Apariciones en tracklist: %d", len(tracklist))
    log.info(
        "Resultados: %s",
        CARPETA_SALIDA.resolve()
    )
    log.info("=" * 65)


def main():

    if len(sys.argv) < 2:
        print(
            'Uso: python dj_analyzer.py "/ruta/a/sesion.mp3"'
        )
        sys.exit(1)

    try:
        archivo = comprobar_archivo(sys.argv[1])
        asyncio.run(analizar_sesion(archivo))

    except KeyboardInterrupt:
        log.warning("Análisis interrumpido por el usuario.")

    except Exception as exc:
        log.exception("Error general: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Monitor de la cartelera de Cines Showcase Argentina, con avisos por Telegram.

Está pensado para correr en GitHub Actions cada ~10 minutos. En cada corrida:

  1. Lee lo que tocaste en Telegram (botones "Seguir" / comandos) usando
     getUpdates y actualiza la lista de películas seguidas.
  2. Abre la cartelera (todoshowcase.com) con un navegador real (Playwright +
     Chromium), cierra el pop-up de promociones si aparece y junta todas las
     películas por su filmid.
  3. Para las películas que lo necesitan (nuevas, seguidas, o que esperan sus
     primeras fechas) abre la página de la película, espera a que termine el
     "cargando funciones..." y lee los días y horarios.
  4. Compara con lo guardado y manda los avisos.
  5. Guarda el estado en state/estado.json y state/seguidas.json. La Action
     después los commitea al repo.

Modos (parámetro --modo):
  normal           Funcionamiento habitual.
  simulacro        Hace todo, pero NO manda mensajes ni guarda estado: solo
                   muestra en el log lo que haría. Ideal para probar.
  probar-telegram  Solo manda un mensaje de prueba para verificar token y chat.
  simular-nueva    (con --filmid) Olvida esa película para que se avise como
                   nueva en esta misma corrida.
  simular-fecha    (con --filmid de una película SEGUIDA) Olvida su última
                   fecha conocida, para que se avise como fecha nueva.
"""

import argparse
import html
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

log = logging.getLogger("showcase")

# ---------------------------------------------------------------------------
# Rutas, URLs y constantes
# ---------------------------------------------------------------------------

RAIZ = Path(__file__).resolve().parent
RUTA_CONFIG = RAIZ / "config.json"
RUTA_ESTADO = RAIZ / "state" / "estado.json"
RUTA_SEGUIDAS = RAIZ / "state" / "seguidas.json"
RUTA_SUCURSALES = RAIZ / "state" / "sucursales.json"

URL_CARTELERA = "https://www.todoshowcase.com/"
URL_PELICULA = "https://entradas.todoshowcase.com/showcase/pelicula?filmid={}"
URL_POSTER = "https://static.voyalcine.net/Uploads/i{}.jpg"

ZONA = ZoneInfo("America/Argentina/Buenos_Aires")
DIAS_SEMANA = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]  # date.weekday(): 0 = lunes
PATRON_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Las 7 sucursales que se pueden activar/desactivar con /sucursales.
# La clave es el id que usa el sitio en los datos de funciones.
SUCURSALES = {
    "11": "Showcase Haedo",
    "12": "Showcase Córdoba (Villa Cabrera)",
    "13": "Showcase Norcenter",
    "14": "Showcase Belgrano",
    "15": "Showcase Quilmes",
    "16": "Showcase Rosario",
    "18": "IMAX Theatre (Norcenter)",
}

CONFIG_POR_DEFECTO = {
    # Solo se usa la PRIMERA vez, para elegir las sucursales activas iniciales
    # (parte del nombre, sin importar mayúsculas ni tildes). "Norcenter" activa
    # "Showcase Norcenter" e "IMAX Theatre (Norcenter)". Lista vacía = todas.
    # Después se eligen desde Telegram con /sucursales.
    "complejos": ["Norcenter"],
    "pausa_entre_paginas_seg": [2, 5],
    "reintentos": 3,
    "minimo_peliculas_esperadas": 5,
    "avisar_tras_fallos_seguidos": 3,
    "dias_a_conservar_fechas_pasadas": 7,
}

ESTADO_INICIAL = {
    "inicializado": False,
    "ultimo_update_id": 0,
    "fallos_seguidos": 0,
    "ultimo_error": None,
    "ultima_corrida_ok": None,
    "peliculas": {},
}


class ErrorScraping(Exception):
    """Algo salió mal leyendo la web del cine."""


class ErrorTelegram(Exception):
    """Algo salió mal hablando con la API de Telegram."""


# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------

def hoy():
    return datetime.now(ZONA).date()


def ahora_iso():
    return datetime.now(ZONA).isoformat(timespec="seconds")


def extraer_filmid(url):
    """Devuelve el filmid de un link (descarta _gl, _ga, house_id y todo lo demás)."""
    try:
        valores = parse_qs(urlparse(url).query).get("filmid")
    except ValueError:
        return None
    if valores and valores[0].strip().isdigit():
        return valores[0].strip()
    return None


def url_limpia(filmid):
    return URL_PELICULA.format(filmid)


def formatear_fecha(iso):
    """'2026-10-15' -> 'Jue 15/10'."""
    d = date.fromisoformat(iso)
    return f"{DIAS_SEMANA[d.weekday()]} {d:%d/%m}"


def lista_fechas(fechas):
    return ", ".join(formatear_fecha(f) for f in sorted(fechas))


def esc(texto):
    """Escapa texto para el modo HTML de Telegram."""
    return html.escape(str(texto), quote=False)


def limpiar_espacios(texto):
    return " ".join((texto or "").split())


def limpiar_secreto(valor):
    """Saca espacios, saltos de línea y comillas que se pueden colar al pegar un Secret."""
    return valor.strip().strip("\"'").strip()


def sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn").lower()


def activas_iniciales(filtros):
    """Traduce el "complejos" del config.json a ids de sucursal (solo la primera vez)."""
    if not filtros:
        return set(SUCURSALES)
    filtros = [sin_tildes(f) for f in filtros]
    return {sid for sid, nombre in SUCURSALES.items() if any(f in sin_tildes(nombre) for f in filtros)}


def agregar_unico(lista, valor):
    if valor and valor not in lista:
        lista.append(valor)


def recortar(texto, largo):
    return texto if len(texto) <= largo else texto[: largo - 1] + "…"


def normalizar_horario(horario):
    """La web marca las funciones de madrugada con 'N ' adelante (ej. 'N 00:30')."""
    horario = limpiar_espacios(horario)
    if horario.startswith("N "):
        return horario[2:] + " (trasnoche)"
    return horario


def leer_json(ruta, defecto):
    if not ruta.exists():
        return defecto
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def escribir_json(ruta, datos):
    """Escribe a un archivo temporal y lo renombra: nunca queda un JSON a medio escribir."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(".tmp")
    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(temporal, ruta)


def partir_texto(texto, limite=4000):
    """Telegram acepta hasta 4096 caracteres por mensaje: partimos por líneas."""
    partes, actual = [], ""
    for linea in texto.split("\n"):
        while len(linea) > limite:
            if actual:
                partes.append(actual)
                actual = ""
            partes.append(linea[:limite])
            linea = linea[limite:]
        if actual and len(actual) + 1 + len(linea) > limite:
            partes.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual.strip():
        partes.append(actual)
    return partes


def con_reintentos(funcion, descripcion, intentos):
    """Ejecuta funcion(); si falla, espera un rato creciente y reintenta."""
    for intento in range(1, intentos + 1):
        try:
            return funcion()
        except Exception as e:  # noqa: BLE001 - queremos reintentar ante cualquier error
            if intento == intentos:
                raise
            espera = 10 * intento + random.uniform(0, 5)
            log.warning("Falló %s (intento %d/%d): %s. Reintento en %.0f s.",
                        descripcion, intento, intentos, e, espera)
            time.sleep(espera)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class Telegram:
    def __init__(self, token, chat_id, simulacro=False):
        self.token = token
        self.chat_id = str(chat_id).strip()
        self.simulacro = simulacro
        self.base = f"https://api.telegram.org/bot{token}/"
        self.sesion = requests.Session()

    def _sin_token(self, texto):
        # Por las dudas: que el token nunca aparezca en los logs.
        return str(texto).replace(self.token, "***") if self.token else str(texto)

    def llamar(self, metodo, reintentos=3, **params):
        params = {k: v for k, v in params.items() if v is not None}
        ultimo_error = None
        for intento in range(1, reintentos + 1):
            try:
                r = self.sesion.post(self.base + metodo, json=params, timeout=40)
                datos = r.json()
            except (requests.RequestException, ValueError) as e:
                ultimo_error = self._sin_token(e)
                log.warning("Telegram %s: error de red (%s). Intento %d/%d.",
                            metodo, ultimo_error, intento, reintentos)
                time.sleep(3 * intento)
                continue
            if datos.get("ok"):
                return datos["result"]
            descripcion = datos.get("description", "sin descripción")
            espera = (datos.get("parameters") or {}).get("retry_after")
            if r.status_code == 429 and espera:  # Telegram pide que vayamos más despacio
                log.warning("Telegram pide esperar %s s.", espera)
                time.sleep(int(espera) + 1)
                ultimo_error = descripcion
                continue
            if r.status_code >= 500:
                ultimo_error = descripcion
                time.sleep(3 * intento)
                continue
            raise ErrorTelegram(f"{metodo}: {descripcion}")
        raise ErrorTelegram(f"{metodo} falló tras {reintentos} intentos: {ultimo_error}")

    @staticmethod
    def _teclado(botones):
        return {"inline_keyboard": botones} if botones else None

    def enviar_mensaje(self, texto, botones=None):
        if self.simulacro:
            log.info("[SIMULACRO] Mensaje:\n%s\n[botones: %s]", texto, _resumen_botones(botones))
            return
        partes = partir_texto(texto)
        for i, parte in enumerate(partes):
            es_ultima = i == len(partes) - 1
            self.llamar("sendMessage", chat_id=self.chat_id, text=parte, parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=self._teclado(botones) if es_ultima else None)

    def enviar_foto(self, url_foto, texto, botones=None):
        if self.simulacro:
            log.info("[SIMULACRO] Foto %s\n%s\n[botones: %s]", url_foto, texto, _resumen_botones(botones))
            return
        if len(texto) <= 1024:  # límite de Telegram para el texto de una foto
            try:
                self.llamar("sendPhoto", chat_id=self.chat_id, photo=url_foto, caption=texto,
                            parse_mode="HTML", reply_markup=self._teclado(botones))
                return
            except ErrorTelegram as e:
                log.warning("No pude mandar el póster (%s). Mando solo el texto.", e)
        else:
            try:
                self.llamar("sendPhoto", reintentos=1, chat_id=self.chat_id, photo=url_foto)
            except ErrorTelegram as e:
                log.warning("No pude mandar el póster (%s).", e)
        self.enviar_mensaje(texto, botones)

    def obtener_updates(self, offset):
        return self.llamar("getUpdates", offset=offset, timeout=0,
                           allowed_updates=["message", "callback_query"])

    def responder_callback(self, callback_id, texto):
        # Normalmente ya pasaron varios minutos y Telegram responde
        # "query is too old": es esperable, lo ignoramos.
        try:
            self.llamar("answerCallbackQuery", reintentos=1,
                        callback_query_id=callback_id, text=texto)
        except ErrorTelegram:
            pass

    def editar_botones(self, chat_id, message_id, botones):
        try:
            self.llamar("editMessageReplyMarkup", reintentos=1, chat_id=chat_id,
                        message_id=message_id, reply_markup={"inline_keyboard": botones or []})
        except ErrorTelegram as e:
            log.info("No pude actualizar los botones del mensaje (%s).", e)


def _resumen_botones(botones):
    if not botones:
        return "ninguno"
    return " | ".join(b.get("text", "?") for fila in botones for b in fila)


# ---------------------------------------------------------------------------
# Contexto de la corrida (configuración + estado + Telegram)
# ---------------------------------------------------------------------------

class Contexto:
    def __init__(self, config, estado, seguidas, activas, tg, simulacro):
        self.config = config
        self.estado = estado
        self.seguidas = seguidas  # {"6028": "Queen Budapest", ...}
        self.activas = activas    # {"13", "18"}: ids de sucursales activas
        self.tg = tg
        self.simulacro = simulacro

    @property
    def peliculas(self):
        return self.estado["peliculas"]

    def guardar(self):
        if self.simulacro:
            return
        self.estado["peliculas"] = dict(sorted(self.peliculas.items(), key=lambda kv: int(kv[0])))
        escribir_json(RUTA_ESTADO, self.estado)
        escribir_json(RUTA_SEGUIDAS, dict(sorted(self.seguidas.items(), key=lambda kv: int(kv[0]))))
        escribir_json(RUTA_SUCURSALES, {"activas": sorted(self.activas, key=int)})

    def nombre_complejos(self):
        if not self.activas:
            return "ninguna sucursal"
        if len(self.activas) == len(SUCURSALES):
            return "todas las sucursales"
        return " / ".join(SUCURSALES[s] for s in sorted(self.activas, key=int))


# ---------------------------------------------------------------------------
# Scraping con Playwright
# ---------------------------------------------------------------------------

# Junta todos los links a películas de la cartelera, con el título y las
# etiquetas (ESTRENO, IMAX, ANTICIPADA...) de la tarjeta donde están.
JS_CARTELERA = """
() => Array.from(document.querySelectorAll("a[href*='filmid=']")).map(a => {
  const caja = a.closest(".boxfilm");
  const etiquetas = caja
    ? Array.from(caja.querySelectorAll(".tag-container > *")).map(e => e.textContent.trim()).filter(Boolean)
    : [];
  const tituloCaja = caja ? ((caja.querySelector(".titulo-pelicula") || {}).textContent || "").trim() : "";
  return { href: a.href, texto: (a.textContent || "").trim(), etiquetas, tituloCaja };
})
"""

# "Ya cargó": hay solapas de días, o apareció el cartel de "no se encontraron funciones".
JS_FUNCIONES_CARGADAS = """
() => {
  const err = document.querySelector('#op_error');
  const errVisible = err && getComputedStyle(err).display !== 'none';
  return document.querySelectorAll('button.op_day').length > 0 || errVisible;
}
"""

# Lee complejos / formatos / horarios de la solapa que está seleccionada.
JS_LEER_COMPLEJOS = """
() => {
  const res = [];
  const cont = document.querySelector('#op_cinemas');
  if (!cont) return res;
  let complejo = null;
  for (const el of cont.children) {
    if (el.tagName === 'H3') { complejo = el.textContent.trim(); continue; }
    if (!complejo) continue;
    let formato = '';
    for (const hijo of el.children) {
      if (hijo.classList.contains('op_format')) {
        formato = hijo.textContent.trim();
      } else if (hijo.classList.contains('op_perfs')) {
        const botones = Array.from(hijo.querySelectorAll('button'));
        const horarios = botones.map(
          b => (b.classList.contains('op_perf_am') ? 'N ' : '') + b.textContent.trim());
        // El id de la sucursal está en onclick="SelectPerformance(438086, 13, '87312A')".
        const m = botones.length
          ? (botones[0].getAttribute('onclick') || '').match(/SelectPerformance\\(\\s*\\d+\\s*,\\s*(\\d+)/)
          : null;
        res.push({ complejo, complejoId: m ? m[1] : '', formato, horarios });
      }
    }
  }
  return res;
}
"""

JS_DIA_SELECCIONADO = """
(dia) => {
  const b = document.querySelector(`button.op_day[value='${dia}']`);
  return !!b && b.classList.contains('op_day_selected');
}
"""

# Último recurso si un pop-up no se deja cerrar: lo escondemos.
JS_OCULTAR_MODALES = """
() => {
  document.querySelectorAll('.modal').forEach(m => {
    if (getComputedStyle(m).display !== 'none') {
      m.style.display = 'none';
      m.classList.remove('in', 'show');
    }
  });
  document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
  document.body.classList.remove('modal-open');
}
"""


def cerrar_popups(page):
    """Cierra el pop-up de promociones (un modal de Bootstrap) si está a la vista."""
    for _ in range(3):
        modales = page.locator(".modal.in:visible, .modal.show:visible")
        if modales.count() == 0:
            break
        log.info("   Pop-up detectado: lo cierro.")
        boton = modales.first.locator(
            "[data-dismiss='modal'], [data-bs-dismiss='modal'], .close, .btn-close")
        try:
            if boton.count() > 0:
                boton.first.click(timeout=3000, force=True)
            else:
                page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            page.keyboard.press("Escape")
        page.wait_for_timeout(1000)
    page.evaluate(JS_OCULTAR_MODALES)


def leer_cartelera(navegador):
    """Devuelve {filmid: {"titulo", "variantes", "etiquetas"}} con lo que hay en la cartelera."""
    page = navegador.new_page()
    try:
        page.goto(URL_CARTELERA, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("a[href*='filmid=']", state="attached", timeout=30000)
        page.wait_for_timeout(2500)  # el pop-up suele aparecer un instante después
        cerrar_popups(page)
        crudos = page.evaluate(JS_CARTELERA)
    finally:
        page.close()

    peliculas = {}
    for item in crudos:
        filmid = extraer_filmid(item.get("href", ""))
        if not filmid:
            continue
        p = peliculas.setdefault(filmid, {"titulos_caja": [], "variantes": [], "etiquetas": []})
        agregar_unico(p["titulos_caja"], limpiar_espacios(item.get("tituloCaja")))
        agregar_unico(p["variantes"], limpiar_espacios(item.get("texto")))
        for etiqueta in item.get("etiquetas") or []:
            agregar_unico(p["etiquetas"], limpiar_espacios(etiqueta).upper())

    resultado = {}
    for filmid, p in peliculas.items():
        titulo = (p["titulos_caja"] or p["variantes"] or [f"Película {filmid}"])[0]
        variantes = p["variantes"][:]
        for t in p["titulos_caja"]:
            agregar_unico(variantes, t)
        resultado[filmid] = {"titulo": titulo, "variantes": variantes, "etiquetas": sorted(p["etiquetas"])}
    return resultado


def leer_funciones(navegador, filmid):
    """
    Abre la página de la película y devuelve
    {"nombre_web": str, "funciones": {"2026-10-15": [{"complejo", "formato", "horarios"}, ...]}}

    Método principal: mientras la página carga, ella misma pide sus funciones
    a un servicio interno (api.voyalcine.net/films/<id>/tree/...) y con esos
    datos dibuja las solapas. Playwright "escucha" esa respuesta y la usa
    directamente: son exactamente los mismos datos que muestran las solapas.
    Respaldo: si no se pudo capturar, recorre las solapas una por una.
    """
    page = navegador.new_page()
    respuestas = []

    def capturar(respuesta):
        url = respuesta.url
        if "voyalcine.net" in url and f"/{filmid}/" in url and "/tree" in url:
            respuestas.append(respuesta)

    page.on("response", capturar)
    try:
        page.goto(url_limpia(filmid), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_function(JS_FUNCIONES_CARGADAS, timeout=40000)  # fin del "cargando funciones..."
        page.wait_for_timeout(800)
        cerrar_popups(page)

        nombre_web = ""
        try:
            nombre_web = limpiar_espacios(page.locator("h2.name").first.text_content(timeout=3000))
        except Exception:  # noqa: BLE001
            pass

        datos, hubo_error_api = None, False
        for r in respuestas:
            if r.ok:
                try:
                    datos = r.json()
                    break
                except Exception:  # noqa: BLE001
                    continue
            else:
                hubo_error_api = True

        # Ojo: una película sin funciones viene con "days" vacío o null; es válido.
        if isinstance(datos, dict) and "days" in datos:
            funciones = normalizar_datos_api(datos)
            nombre_web = nombre_web or limpiar_espacios(datos.get("name"))
        elif page.locator("button.op_day").count() > 0:
            log.info("   No capturé los datos directos; leo las solapas una por una.")
            funciones = leer_solapas(page)
        elif hubo_error_api:
            raise ErrorScraping("la página dice 'sin funciones' pero el servidor respondió con error")
        else:
            funciones = {}  # la página dice "No se encontraron funciones"
        return {"nombre_web": nombre_web, "funciones": funciones}
    finally:
        page.close()


def normalizar_datos_api(datos):
    funciones = {}
    for dia, complejos in (datos.get("days") or {}).items():
        if not PATRON_FECHA.match(str(dia)):
            continue
        entradas = []
        for c in complejos or []:
            for f in c.get("formats") or []:
                horarios = [normalizar_horario(p.get("showTime", "")) for p in f.get("performances") or []]
                horarios = [h for h in horarios if h]
                if horarios:
                    entradas.append({
                        "complejo": limpiar_espacios(c.get("name")) or "?",
                        "complejo_id": str(c.get("id", "")),
                        "formato": limpiar_espacios(f.get("formatDescription")),
                        "horarios": horarios,
                    })
        if entradas:
            funciones[dia] = entradas
    return funciones


def leer_solapas(page):
    """Respaldo: hace clic en cada solapa de día y lee sus horarios."""
    # Se usa el atributo value="2026-10-15" del botón, no el texto ("Hoy",
    # "Jue 15/10"), así no importa qué solapa esté resaltada o parpadeando.
    dias = page.eval_on_selector_all("button.op_day", "els => els.map(e => e.value)")
    funciones = {}
    for dia in dias:
        if not PATRON_FECHA.match(dia or ""):
            continue
        cerrar_popups(page)
        page.click(f"button.op_day[value='{dia}']", timeout=10000)
        page.wait_for_function(JS_DIA_SELECCIONADO, arg=dia, timeout=15000)
        page.wait_for_timeout(600)
        entradas = []
        for bloque in page.evaluate(JS_LEER_COMPLEJOS):
            horarios = [normalizar_horario(h) for h in bloque["horarios"]]
            horarios = [h for h in horarios if h]
            if horarios:
                entradas.append({"complejo": limpiar_espacios(bloque["complejo"]),
                                 "complejo_id": str(bloque.get("complejoId") or ""),
                                 "formato": limpiar_espacios(bloque["formato"]),
                                 "horarios": horarios})
        if entradas:
            funciones[dia] = entradas
        page.wait_for_timeout(random.randint(400, 900))
    return funciones


def filtrar_complejos(funciones, activas):
    """Deja solo las sucursales activas (y los días en que les quedan funciones)."""
    resultado = {}
    for dia, entradas in funciones.items():
        mias = [e for e in entradas if e.get("complejo_id") in activas]
        if mias:
            resultado[dia] = mias
    return resultado


def fechas_por_sucursal(funciones):
    """{"13": {"2026-10-15", ...}, ...} solo para las 7 sucursales del catálogo."""
    resultado = {}
    for dia, entradas in funciones.items():
        for e in entradas:
            if e.get("complejo_id") in SUCURSALES:
                resultado.setdefault(e["complejo_id"], set()).add(dia)
    return resultado


# ---------------------------------------------------------------------------
# Botones y textos de los mensajes
# ---------------------------------------------------------------------------

def teclado_pelicula(filmid, seguida):
    if seguida:
        boton = {"text": "✅ Siguiendo (tocá para dejar de seguir)", "callback_data": f"dejar:{filmid}|n"}
    else:
        boton = {"text": "➕ Seguir esta película", "callback_data": f"seguir:{filmid}|n"}
    return [[boton], [{"text": "🎟 Ver en la web", "url": url_limpia(filmid)}]]


def teclado_link(filmid):
    return [[{"text": "🎟 Ver en la web", "url": url_limpia(filmid)}]]


def teclado_cartelera(ctx):
    en_cartelera = [(fid, p) for fid, p in ctx.peliculas.items() if p.get("en_cartelera")]
    en_cartelera.sort(key=lambda kv: kv[1]["titulo"].lower())
    filas = []
    for fid, p in en_cartelera[:95]:  # Telegram admite hasta 100 botones
        if fid in ctx.seguidas:
            filas.append([{"text": "✅ " + recortar(p["titulo"], 45), "callback_data": f"dejar:{fid}|c"}])
        else:
            filas.append([{"text": "➕ " + recortar(p["titulo"], 45), "callback_data": f"seguir:{fid}|c"}])
    return filas


def teclado_seguidas(ctx):
    return [[{"text": "❌ " + recortar(titulo, 45), "callback_data": f"dejar:{fid}|s"}]
            for fid, titulo in sorted(ctx.seguidas.items(), key=lambda kv: kv[1].lower())]


def teclado_sucursales(ctx):
    filas = []
    for sid, nombre in SUCURSALES.items():
        marca = "✅" if sid in ctx.activas else "❌"
        filas.append([{"text": f"{marca} {nombre}", "callback_data": f"suc:{sid}"}])
    return filas


def lineas_titulo(p):
    lineas = [f"<b>{esc(p['titulo'])}</b>"]
    if p.get("etiquetas"):
        lineas.append("🏷 " + esc(" · ".join(p["etiquetas"])))
    otras = [v for v in p.get("variantes", []) if v and v != p["titulo"]]
    if otras:
        lineas.append("📝 En la web figura también como: " + esc(" / ".join(otras)))
    return lineas


def lineas_horarios(funciones, fechas):
    lineas = []
    for dia in sorted(fechas):
        lineas.append("")
        lineas.append(f"<b>📅 {formatear_fecha(dia)}</b>")
        for e in funciones.get(dia, []):
            formato = f" ({esc(e['formato'])})" if e["formato"] else ""
            lineas.append(f"• {esc(e['complejo'])}{formato}: {esc(', '.join(e['horarios']))}")
    return lineas


def mensaje_nueva(ctx, p, mias):
    lineas = ["🎬 <b>NUEVA EN CARTELERA</b>", ""] + lineas_titulo(p) + [""]
    if mias:
        lineas.append(f"📅 Fechas en {esc(ctx.nombre_complejos())}: {lista_fechas(mias)}")
    else:
        lineas.append("📅 Todavía sin funciones cargadas. Te aviso cuando aparezcan.")
    return "\n".join(lineas)


def mensaje_primeras_fechas(ctx, p, fechas):
    return "\n".join([
        f"📅 <b>Ya tiene fechas:</b> {esc(p['titulo'])}",
        f"En {esc(ctx.nombre_complejos())}: {lista_fechas(fechas)}",
        "",
        "Si querés que te avise cuando agreguen más (con horarios), seguila.",
    ])


def mensaje_fechas_nuevas(ctx, p, mias, nuevas):
    lineas = [f"🆕 <b>Fechas nuevas: {esc(p['titulo'])}</b>",
              f"({esc(ctx.nombre_complejos())})"]
    return "\n".join(lineas + lineas_horarios(mias, nuevas))


def mensaje_estado_actual(ctx, p, mias):
    futuras = [f for f in mias if f >= hoy().isoformat()]
    lineas = [f"📋 <b>{esc(p['titulo'])}</b>: fechas y horarios actuales en {esc(ctx.nombre_complejos())}"]
    if futuras:
        lineas += lineas_horarios(mias, futuras)
        lineas += ["", "Te aviso cuando agreguen fechas nuevas."]
    else:
        lineas += ["", "Todavía no tiene funciones ahí. Te aviso apenas carguen."]
    return "\n".join(lineas)


def texto_ayuda(ctx):
    return "\n".join([
        "🤖 <b>Monitor de cartelera Showcase</b>",
        "",
        "Te aviso cuando aparece una película nueva y cuando le agregan fechas "
        f"a las que seguís, solo en tus sucursales activas ({esc(ctx.nombre_complejos())}).",
        "",
        "/cartelera – lista para seguir o dejar de seguir películas",
        "/seguidas – las películas que seguís",
        "/sucursales – elegir de qué sucursales querés avisos",
        "/ayuda – este mensaje",
        "",
        "ℹ️ No respondo al instante: leo tus botones y comandos en la próxima "
        "revisión (cada ~10 minutos, a veces un poco más).",
    ])


# ---------------------------------------------------------------------------
# Telegram: procesar botones y comandos
# ---------------------------------------------------------------------------

def procesar_telegram(ctx):
    try:
        updates = ctx.tg.obtener_updates(ctx.estado["ultimo_update_id"] + 1)
    except ErrorTelegram as e:
        log.warning("No pude leer Telegram (%s). Sigo con la cartelera igual.", e)
        return
    if updates:
        log.info("Telegram: %d interacciones nuevas.", len(updates))
    for update in updates:
        try:
            procesar_update(ctx, update)
        except Exception:  # noqa: BLE001
            log.exception("Error procesando la interacción %s", update.get("update_id"))
        ctx.estado["ultimo_update_id"] = max(ctx.estado["ultimo_update_id"], update["update_id"])
        ctx.guardar()


def procesar_update(ctx, update):
    if "callback_query" in update:
        cq = update["callback_query"]
        mensaje = cq.get("message") or {}
        chat_id = str((mensaje.get("chat") or {}).get("id", ""))
        if chat_id != ctx.tg.chat_id:
            log.warning("Ignoro un botón tocado desde otro chat (%s).", chat_id)
            return
        accion, _, resto = (cq.get("data") or "").partition(":")
        if accion == "suc":
            cambiar_sucursal(ctx, resto)
            ctx.tg.responder_callback(cq["id"], "Listo")
            if mensaje.get("message_id"):
                ctx.tg.editar_botones(chat_id, mensaje["message_id"], teclado_sucursales(ctx))
            return
        filmid, _, origen = resto.partition("|")
        if not filmid.isdigit():
            return
        if accion == "seguir":
            seguir(ctx, filmid)
            ctx.tg.responder_callback(cq["id"], "Listo, la sigo")
        elif accion == "dejar":
            dejar_de_seguir(ctx, filmid)
            ctx.tg.responder_callback(cq["id"], "Listo, la dejé de seguir")
        else:
            return
        # Actualizamos los botones del mensaje donde tocaste.
        if mensaje.get("message_id"):
            if origen == "c":
                botones = teclado_cartelera(ctx)
            elif origen == "s":
                botones = teclado_seguidas(ctx)
            else:
                botones = teclado_pelicula(filmid, filmid in ctx.seguidas)
            ctx.tg.editar_botones(chat_id, mensaje["message_id"], botones)

    elif "message" in update:
        mensaje = update["message"]
        chat_id = str((mensaje.get("chat") or {}).get("id", ""))
        if chat_id != ctx.tg.chat_id:
            log.warning("Ignoro un mensaje de otro chat (%s).", chat_id)
            return
        texto = (mensaje.get("text") or "").strip()
        comando = texto.split()[0].split("@")[0].lower() if texto else ""
        if comando == "/cartelera":
            botones = teclado_cartelera(ctx)
            if botones:
                ctx.tg.enviar_mensaje("🎞 <b>Cartelera actual</b>\n➕ = tocá para seguir · ✅ = la seguís "
                                      "(tocá para dejar de seguir)", botones)
            else:
                ctx.tg.enviar_mensaje("Todavía no leí la cartelera. Probá de nuevo en unos minutos.")
        elif comando == "/seguidas":
            if ctx.seguidas:
                ctx.tg.enviar_mensaje("⭐ <b>Películas que seguís</b>\nTocá una para dejar de seguirla.",
                                      teclado_seguidas(ctx))
            else:
                ctx.tg.enviar_mensaje("No seguís ninguna película. Usá /cartelera para elegir.")
        elif comando == "/sucursales":
            ctx.tg.enviar_mensaje("🏢 <b>Sucursales</b>\n✅ = activa (te aviso) · ❌ = inactiva\n"
                                  "Tocá una para cambiarla.", teclado_sucursales(ctx))
        else:  # /start, /ayuda o cualquier otra cosa
            ctx.tg.enviar_mensaje(texto_ayuda(ctx))


def seguir(ctx, filmid):
    p = ctx.peliculas.get(filmid)
    titulo = p["titulo"] if p else f"Película {filmid}"
    if filmid in ctx.seguidas:
        ctx.tg.enviar_mensaje(f"Ya seguías <b>{esc(titulo)}</b>.")
        return
    ctx.seguidas[filmid] = titulo
    if p:
        # En la próxima lectura tomamos sus fechas actuales como punto de
        # partida y te las mandamos, en vez de avisarlas como "nuevas".
        p["rebasar"] = True
        p["confirmar_seguimiento"] = True
    log.info("Ahora se sigue %s (%s).", filmid, titulo)
    ctx.tg.enviar_mensaje(f"✅ Ahora seguís <b>{esc(titulo)}</b>.\n"
                          "En un momento te paso sus fechas y horarios actuales.")


def cambiar_sucursal(ctx, sid):
    if sid not in SUCURSALES:
        return
    if sid in ctx.activas:
        ctx.activas.discard(sid)
        accion = "❌ Desactivaste"
    else:
        ctx.activas.add(sid)
        accion = "✅ Activaste"
    log.info("%s la sucursal %s.", accion, SUCURSALES[sid])
    ctx.tg.enviar_mensaje(f"{accion} <b>{esc(SUCURSALES[sid])}</b>.\n"
                          f"Activas ahora: {esc(ctx.nombre_complejos())}.")


def dejar_de_seguir(ctx, filmid):
    titulo = ctx.seguidas.pop(filmid, None)
    p = ctx.peliculas.get(filmid)
    if p:
        p["confirmar_seguimiento"] = False
        titulo = titulo or p["titulo"]
    log.info("Se deja de seguir %s.", filmid)
    ctx.tg.enviar_mensaje(f"🗑 Dejaste de seguir <b>{esc(titulo or filmid)}</b>.")


# ---------------------------------------------------------------------------
# Lógica principal: comparar y avisar
# ---------------------------------------------------------------------------

def entrada_nueva(filmid, info_cartelera, resultado):
    info = info_cartelera or {}
    return {
        "titulo": info.get("titulo") or resultado.get("nombre_web") or f"Película {filmid}",
        "variantes": info.get("variantes", []),
        "etiquetas": info.get("etiquetas", []),
        "nombre_web": resultado.get("nombre_web", ""),
        "en_cartelera": bool(info_cartelera),
        "visto_por_primera_vez": ahora_iso(),
        "fechas_conocidas": {},  # {"13": ["2026-10-15", ...], ...} por sucursal
        "esperando_primeras_fechas": False,
        "pendiente_aviso_nueva": False,  # vista, pero solo en sucursales inactivas
        "rebasar": False,
        "confirmar_seguimiento": False,
    }


def unir_fechas(conocidas, actuales):
    """Suma las fechas actuales (por sucursal) a las conocidas."""
    resultado = {sid: set(fechas) for sid, fechas in conocidas.items()}
    for sid, fechas in actuales.items():
        resultado.setdefault(sid, set()).update(fechas)
    return {sid: sorted(f) for sid, f in sorted(resultado.items(), key=lambda kv: int(kv[0])) if f}


def procesar_pelicula(ctx, filmid, info_cartelera, resultado, inicial):
    """
    Compara lo leído con lo guardado y manda el aviso que corresponda.
    Regla de oro: el estado se actualiza DESPUÉS de mandar el aviso. Si
    Telegram falla, no se marca nada y en la próxima corrida se reintenta.
    """
    todas = resultado["funciones"]
    mias = filtrar_complejos(todas, ctx.activas)
    fechas_mias = set(mias)
    actuales = fechas_por_sucursal(todas)  # se guardan las 7, activas o no
    hoy_iso = hoy().isoformat()
    p = ctx.peliculas.get(filmid)

    # 1) Película que nunca habíamos visto.
    if p is None:
        p = entrada_nueva(filmid, info_cartelera, resultado)
        # Se avisa si tiene funciones en tus sucursales activas, o si no tiene
        # funciones en ningún lado todavía (ej. anticipadas). Si solo está en
        # sucursales inactivas, se registra sin avisar y queda pendiente.
        avisar = bool(not inicial and ctx.activas and (fechas_mias or not todas))
        if avisar:
            log.info("   NUEVA: %s (%s)", p["titulo"], filmid)
            ctx.tg.enviar_foto(URL_POSTER.format(filmid), mensaje_nueva(ctx, p, fechas_mias),
                               teclado_pelicula(filmid, filmid in ctx.seguidas))
        elif not inicial:
            log.info("   NUEVA pero sin funciones en tus sucursales: %s. Queda pendiente.", p["titulo"])
            p["pendiente_aviso_nueva"] = True
        p["fechas_conocidas"] = unir_fechas({}, actuales)
        # Sin funciones en ningún lado (ej. anticipada): queda esperando sus primeras fechas.
        p["esperando_primeras_fechas"] = (avisar or (inicial and not todas)) and not fechas_mias
        ctx.peliculas[filmid] = p
        return

    if resultado.get("nombre_web"):
        p["nombre_web"] = resultado["nombre_web"]

    # 2) La acabás de seguir (o quedó pendiente una lectura base).
    if p.get("rebasar"):
        if p.get("confirmar_seguimiento") and filmid in ctx.seguidas:
            ctx.tg.enviar_mensaje(mensaje_estado_actual(ctx, p, mias), teclado_link(filmid))
        p["fechas_conocidas"] = unir_fechas(p.get("fechas_conocidas", {}), actuales)
        p["rebasar"] = False
        p["confirmar_seguimiento"] = False
        return

    # 3) Estaba pendiente (solo en sucursales inactivas) y ahora aparece en una activa.
    if p.get("pendiente_aviso_nueva"):
        if fechas_mias:
            log.info("   NUEVA (ahora en tus sucursales): %s (%s)", p["titulo"], filmid)
            ctx.tg.enviar_foto(URL_POSTER.format(filmid), mensaje_nueva(ctx, p, fechas_mias),
                               teclado_pelicula(filmid, filmid in ctx.seguidas))
            p["pendiente_aviso_nueva"] = False
        p["fechas_conocidas"] = unir_fechas(p.get("fechas_conocidas", {}), actuales)
        return

    # Fechas nuevas = fechas de sucursales ACTIVAS que antes no tenían.
    conocidas = p.get("fechas_conocidas", {})
    nuevas = set()
    for sid in ctx.activas:
        nuevas |= actuales.get(sid, set()) - set(conocidas.get(sid, []))
    nuevas = {f for f in nuevas if f >= hoy_iso}

    # 4) Película seguida con fechas nuevas: aviso con horarios.
    if filmid in ctx.seguidas:
        if nuevas:
            log.info("   FECHAS NUEVAS en seguida %s: %s", p["titulo"], sorted(nuevas))
            ctx.tg.enviar_mensaje(mensaje_fechas_nuevas(ctx, p, mias, nuevas), teclado_link(filmid))
    # 5) No seguida, pero estaba sin fechas y ahora tiene en tus sucursales: aviso único.
    elif p.get("esperando_primeras_fechas") and nuevas:
        log.info("   PRIMERAS FECHAS en %s: %s", p["titulo"], sorted(nuevas))
        ctx.tg.enviar_mensaje(mensaje_primeras_fechas(ctx, p, nuevas), teclado_pelicula(filmid, False))

    p["fechas_conocidas"] = unir_fechas(conocidas, actuales)
    if fechas_mias:
        p["esperando_primeras_fechas"] = False


def actualizar_presencia(ctx, cartelera):
    """Marca qué películas siguen en cartelera y refresca títulos y etiquetas."""
    for filmid, p in ctx.peliculas.items():
        info = cartelera.get(filmid)
        p["en_cartelera"] = info is not None
        if info:
            p["titulo"] = info["titulo"]
            p["variantes"] = info["variantes"]
            p["etiquetas"] = info["etiquetas"]
            if filmid in ctx.seguidas:
                ctx.seguidas[filmid] = info["titulo"]


def podar_fechas_viejas(ctx):
    limite = (hoy() - timedelta(days=ctx.config["dias_a_conservar_fechas_pasadas"])).isoformat()
    for p in ctx.peliculas.values():
        p["fechas_conocidas"] = {sid: [f for f in fechas if f >= limite]
                                 for sid, fechas in p.get("fechas_conocidas", {}).items()
                                 if any(f >= limite for f in fechas)}


def migrar_estado(ctx):
    """
    Versiones anteriores guardaban fechas_conocidas como una lista (ya filtrada
    por complejo). Ahora es por sucursal: esas películas se releen una vez
    "en silencio" para armar la base nueva, sin mandarte avisos.
    """
    migradas = 0
    for p in ctx.peliculas.values():
        if isinstance(p.get("fechas_conocidas"), list):
            p["fechas_conocidas"] = {}
            p["rebasar"] = True
            migradas += 1
        p.setdefault("pendiente_aviso_nueva", False)
    if migradas:
        log.info("Migración: %d películas se releen una vez para guardar fechas por sucursal.", migradas)


def aplicar_modo_prueba(ctx, modo, filmid):
    if modo not in ("simular-nueva", "simular-fecha"):
        return
    if not ctx.estado["inicializado"]:
        raise SystemExit("Primero tiene que haber corrido al menos una vez en modo normal.")
    if not filmid or not filmid.isdigit():
        raise SystemExit(f"El modo {modo} necesita un filmid (número).")
    if modo == "simular-nueva":
        ctx.peliculas.pop(filmid, None)
        log.info("PRUEBA: olvidé la película %s; se va a avisar como nueva.", filmid)
    else:
        p = ctx.peliculas.get(filmid)
        if filmid not in ctx.seguidas or not p:
            raise SystemExit(f"La película {filmid} no está en tus seguidas.")
        conocidas = p.get("fechas_conocidas")
        if not isinstance(conocidas, dict):
            raise SystemExit("Primero tiene que correr una vez en modo normal con esta versión.")
        futuras = [f for sid in ctx.activas for f in conocidas.get(sid, []) if f >= hoy().isoformat()]
        if not futuras:
            raise SystemExit(f"La película {filmid} no tiene fechas futuras en tus sucursales activas.")
        ultima = max(futuras)
        for sid in ctx.activas:
            if ultima in conocidas.get(sid, []):
                conocidas[sid].remove(ultima)
        p["rebasar"] = False
        log.info("PRUEBA: olvidé la fecha %s de %s; se va a avisar como nueva.", ultima, filmid)


def correr(ctx, modo, filmid_prueba):
    cfg = ctx.config
    migrar_estado(ctx)
    if not ctx.simulacro:
        procesar_telegram(ctx)
    aplicar_modo_prueba(ctx, modo, filmid_prueba)

    inicial = not ctx.estado["inicializado"]
    fallidas = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            navegador = browser.new_context(
                locale="es-AR", timezone_id="America/Argentina/Buenos_Aires",
                user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})

            log.info("Leyendo la cartelera…")
            cartelera = con_reintentos(lambda: leer_cartelera(navegador), "leer la cartelera",
                                       cfg["reintentos"])
            if len(cartelera) < cfg["minimo_peliculas_esperadas"]:
                # Algo raro (página caída, cambió el diseño...). No tocamos nada.
                raise ErrorScraping(f"la cartelera trajo solo {len(cartelera)} películas")
            log.info("Cartelera: %d películas.", len(cartelera))
            actualizar_presencia(ctx, cartelera)

            # ¿Qué películas hay que abrir? Las nuevas, las seguidas y las que esperan fechas.
            a_leer = []
            for fid in cartelera:
                if fid not in ctx.peliculas:
                    agregar_unico(a_leer, fid)
            for fid in ctx.seguidas:
                agregar_unico(a_leer, fid)
            for fid, p in ctx.peliculas.items():
                esperando = p.get("esperando_primeras_fechas") or p.get("pendiente_aviso_nueva")
                if p.get("rebasar") or (esperando and p.get("en_cartelera")):
                    agregar_unico(a_leer, fid)
            log.info("Películas a revisar en detalle: %d.", len(a_leer))

            for i, fid in enumerate(a_leer):
                if i:
                    time.sleep(random.uniform(*cfg["pausa_entre_paginas_seg"]))  # ser cortés con el servidor
                log.info("-> %s (%s)", cartelera.get(fid, {}).get("titulo")
                         or ctx.peliculas.get(fid, {}).get("titulo", "?"), fid)
                try:
                    resultado = con_reintentos(lambda: leer_funciones(navegador, fid),
                                               f"leer la película {fid}", cfg["reintentos"])
                    log.info("   %d días en total, %d en tus sucursales activas.", len(resultado["funciones"]),
                             len(filtrar_complejos(resultado["funciones"], ctx.activas)))
                except Exception as e:  # noqa: BLE001
                    log.error("   No pude leer la película %s: %s", fid, e)
                    fallidas.append(fid)
                    continue
                try:
                    procesar_pelicula(ctx, fid, cartelera.get(fid), resultado, inicial)
                except ErrorTelegram as e:
                    log.error("   No pude mandar el aviso de %s (%s). Se reintenta en la próxima corrida.", fid, e)
                ctx.guardar()
        finally:
            browser.close()

    if inicial:
        # Las que no se pudieron leer en la primera corrida se registran igual
        # (sin aviso) y se leen como "base" en la próxima.
        for fid in fallidas:
            if fid not in ctx.peliculas:
                p = entrada_nueva(fid, cartelera.get(fid), {})
                p["rebasar"] = True
                ctx.peliculas[fid] = p
        ctx.estado["inicializado"] = True
        ctx.tg.enviar_mensaje(
            f"✅ <b>Monitor de Showcase iniciado</b>\nRegistré {len(ctx.peliculas)} películas en cartelera "
            "(no te aviso de cada una para no llenarte de mensajes). Desde ahora te aviso las novedades.\n\n"
            f"Sucursales activas: {esc(ctx.nombre_complejos())}.\n\n"
            "Mandá /cartelera para elegir qué películas seguir y /sucursales para elegir sucursales.")

    podar_fechas_viejas(ctx)
    ctx.guardar()

    if a_leer and len(fallidas) == len(a_leer):
        raise ErrorScraping("no se pudo leer ninguna página de película")


def registrar_fallo(ctx, error):
    """Cuenta fallos seguidos y te avisa por Telegram una sola vez si se repiten."""
    ctx.estado["fallos_seguidos"] = ctx.estado.get("fallos_seguidos", 0) + 1
    ctx.estado["ultimo_error"] = str(error)[:300]
    umbral = ctx.config["avisar_tras_fallos_seguidos"]
    if ctx.estado["fallos_seguidos"] == umbral:
        try:
            ctx.tg.enviar_mensaje(
                f"⚠️ El monitor de Showcase falló {umbral} veces seguidas.\n"
                f"Último error: <code>{esc(str(error)[:300])}</code>\n"
                "Mirá la pestaña Actions del repo. Te aviso cuando vuelva a andar.")
        except ErrorTelegram:
            pass
    ctx.guardar()
    return ctx.estado["fallos_seguidos"] == umbral


def registrar_exito(ctx):
    if ctx.estado.get("fallos_seguidos", 0) >= ctx.config["avisar_tras_fallos_seguidos"]:
        try:
            ctx.tg.enviar_mensaje("✅ El monitor de Showcase volvió a andar.")
        except ErrorTelegram:
            pass
    ctx.estado["fallos_seguidos"] = 0
    ctx.estado["ultimo_error"] = None
    # Solo la fecha (no la hora) para no generar un commit en cada corrida.
    ctx.estado["ultima_corrida_ok"] = hoy().isoformat()
    ctx.guardar()


def probar_telegram(tg):
    try:
        yo = tg.llamar("getMe")
    except ErrorTelegram as e:
        if "Unauthorized" in str(e):
            log.error("Telegram rechazó el token (Unauthorized). El TELEGRAM_TOKEN guardado en GitHub "
                      "no es válido: revisá que sea exactamente el que da BotFather (/mybots → tu bot → "
                      "API Token), sin 'bot' adelante, sin comillas, y que no lo hayas regenerado.")
        raise
    log.info("Token OK: el bot es @%s", yo.get("username"))
    tg.enviar_mensaje("🧪 <b>Prueba del monitor de Showcase</b>\nSi ves este mensaje, el token y el chat id "
                      "están bien configurados.", [[{"text": "🎟 Abrir Showcase", "url": URL_CARTELERA}]])
    log.info("Mensaje de prueba enviado al chat %s.", tg.chat_id)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    parser = argparse.ArgumentParser(description="Monitor de cartelera Showcase")
    parser.add_argument("--modo", default="normal",
                        choices=["normal", "simulacro", "probar-telegram", "simular-nueva", "simular-fecha"])
    parser.add_argument("--filmid", default="")
    args = parser.parse_args()
    modo = args.modo or "normal"
    simulacro = modo == "simulacro"

    config = {**CONFIG_POR_DEFECTO, **leer_json(RUTA_CONFIG, {})}
    estado = {**ESTADO_INICIAL, **leer_json(RUTA_ESTADO, {})}
    seguidas = {str(k): str(v) for k, v in leer_json(RUTA_SEGUIDAS, {}).items()}
    # Sucursales activas: del archivo de estado si existe; si no (primera vez),
    # se toman del "complejos" de config.json.
    guardadas = leer_json(RUTA_SUCURSALES, None)
    if isinstance(guardadas, dict) and isinstance(guardadas.get("activas"), list):
        activas = {str(s) for s in guardadas["activas"] if str(s) in SUCURSALES}
    else:
        activas = activas_iniciales(config["complejos"])
        log.info("Sucursales activas iniciales (desde config.json): %s",
                 ", ".join(SUCURSALES[s] for s in sorted(activas, key=int)) or "ninguna")

    token = limpiar_secreto(os.environ.get("TELEGRAM_TOKEN", ""))
    if token.lower().startswith("bot"):
        token = token[3:]  # error común: copiar el "bot" de la URL junto con el token
    chat_id = limpiar_secreto(os.environ.get("TELEGRAM_CHAT_ID", ""))
    if token and not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
        # No mostramos el token: solo pistas de su forma.
        log.warning("El TELEGRAM_TOKEN no tiene el formato esperado 'números:letras' "
                    "(largo %d, tiene ':' = %s, tiene espacios = %s). Revisá el Secret.",
                    len(token), ":" in token, " " in token)
    if not simulacro and (not token or not chat_id):
        log.error("Faltan TELEGRAM_TOKEN y/o TELEGRAM_CHAT_ID (cargalos como Secrets en GitHub).")
        return 1

    tg = Telegram(token, chat_id, simulacro=simulacro)
    if simulacro:
        log.info("MODO SIMULACRO: no se manda nada a Telegram ni se guarda el estado.")
    if modo == "probar-telegram":
        probar_telegram(tg)
        return 0

    ctx = Contexto(config, estado, seguidas, activas, tg, simulacro)
    try:
        correr(ctx, modo, args.filmid.strip())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("La corrida falló: %s", e)
        # Devolvemos error (corrida en rojo) solo la vez que se llega al umbral
        # de fallos seguidos, para que GitHub no te mande un mail cada 10 minutos.
        return 1 if registrar_fallo(ctx, e) else 0
    registrar_exito(ctx)
    log.info("Listo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

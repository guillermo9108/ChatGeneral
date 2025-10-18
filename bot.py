import os
import time
import imaplib
import email
import smtplib
from flask import Flask, jsonify
import threading
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import traceback
import signal
import sys
import json
from email.utils import parseaddr, formatdate
import uuid

# Configuración de logging mejorada con rotación
logging.basicConfig(
    level=logging.DEBUG,  # DEBUG para desarrollo, INFO para producción
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler('youchat_bot.log', maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Clase para logging estructurado
class StructuredLogger:
    def __init__(self, logger):
        self.logger = logger

    def info(self, message, extra=None):
        log_data = {"message": message, **(extra or {})}
        self.logger.info(json.dumps(log_data))

    def error(self, message, extra=None):
        log_data = {"message": message, **(extra or {})}
        self.logger.error(json.dumps(log_data))

structured_logger = StructuredLogger(logger)

app = Flask(__name__)

# =============================================================================
# CONFIGURACIÓN DE CORREO Y CONSTANTES
# =============================================================================
# Configuración IMAP
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", 993))
IMAP_USER = os.getenv("IMAP_USER", "youchatbotpy@gmail.com")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "wopahppfgakptilr")

# Variables Globales Derivadas y Constantes
# Las variables usadas en el logging y las rutas de Flask estaban faltando
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT", IMAP_USER)
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", IMAP_PASSWORD)
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 10)) # Intervalo de verificación en segundos (e.g., 10 segundos)

# =============================================================================
# Manejador de señales para cerrar conexiones limpiamente
# ... (el resto del código sigue igual)
# =============================================================================
def signal_handler(sig, frame):
    structured_logger.info("Recibida señal de terminación, cerrando conexiones")
    youchat_bot.is_running = False
    youchat_bot.cerrar_conexion_imap()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# =============================================================================
# FUNCIONES DEL BOT YOUCHAT
# =============================================================================
class YouChatBot:
    def __init__(self):
        self.is_running = False
        self.last_check = None
        self.processed_emails = set()
        self.total_processed = 0
        self.emails_sent_today = 0
        self.last_reset = datetime.now().date()
        self.imap_connection = None
        self.last_reconnect = None

    def reset_email_count(self):
        if datetime.now().date() != self.last_reset:
            self.emails_sent_today = 0
            self.last_reset = datetime.now().date()

    def conectar_imap_robusto(self):
        """Conexión IMAP robusta con manejo de errores"""
        try:
            if self.imap_connection:
                try:
                    self.imap_connection.noop()
                    structured_logger.info("Conexión IMAP aún activa")
                    return self.imap_connection
                except:
                    structured_logger.info("Conexión IMAP perdida, reconectando")
                    self.imap_connection = None

            structured_logger.info("Estableciendo nueva conexión IMAP")
            mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
            mail.select("inbox")
            self.imap_connection = mail
            self.last_reconnect = datetime.now()
            structured_logger.info("Conexión IMAP establecida exitosamente")
            return mail
        except Exception as e:
            structured_logger.error("Error crítico en conexión IMAP", {"error": str(e), "traceback": traceback.format_exc()})
            self.imap_connection = None
            return None

    def verificar_conexion_imap(self):
        """Verifica y mantiene la conexión IMAP activa"""
        try:
            if not self.imap_connection:
                return self.conectar_imap_robusto()

            if self.last_reconnect and (datetime.now() - self.last_reconnect).seconds > 600:
                structured_logger.info("Reconexión programada IMAP")
                self.cerrar_conexion_imap()
                return self.conectar_imap_robusto()

            self.imap_connection.noop()
            return self.imap_connection
        except Exception as e:
            structured_logger.info("Conexión IMAP necesita reconexión", {"error": str(e)})
            self.cerrar_conexion_imap()
            return self.conectar_imap_robusto()

    def cerrar_conexion_imap(self):
        """Cierra la conexión IMAP de forma segura"""
        try:
            if self.imap_connection:
                self.imap_connection.close()
                self.imap_connection.logout()
                self.imap_connection = None
                structured_logger.info("Conexión IMAP cerrada")
        except:
            self.imap_connection = None

    def check_smtp_health(self):
        """Verifica la conectividad con el servidor SMTP"""
        try:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as servidor:
                servidor.noop()
            structured_logger.info("Servidor SMTP accesible")
            return True
        except Exception as e:
            structured_logger.error("Verificación de salud SMTP falló", {"error": str(e)})
            return False

    def extraer_headers_youchat(self, mensaje_email):
        """Extrae headers específicos de YouChat"""
        headers_youchat = {}
        headers_especificos = [
            'Message-ID', 'Msg_id', 'Chat-Version', 'Pd',
            'Sender-Alias', 'From-Alias', 'Thread-ID', 'User-ID',
            'X-YouChat-Session', 'X-YouChat-Platform', 'X-YouChat-Device',
            'Chat-ID', 'Session-ID', 'X-Chat-Signature', 'X-YouChat-Version',
            'X-YouChat-Build', 'X-YouChat-Environment'
        ]

        for header, valor in mensaje_email.items():
            header_lower = header.lower()
            if any(keyword in header_lower for keyword in ['youchat', 'chat-', 'msg_', 'x-youchat', 'x-chat']):
                headers_youchat[header] = valor
            elif header in headers_especificos:
                headers_youchat[header] = valor

        structured_logger.info("Headers de YouChat extraídos", {"headers": list(headers_youchat.keys())})
        return headers_youchat

    def extraer_email_remitente(self, remitente):
        """Extrae el email del remitente de forma robusta"""
        try:
            _, email = parseaddr(remitente)
            if email:
                return email.strip()
            structured_logger.error("Remitente sin email válido", {"remitente": remitente})
            return None
        except Exception as e:
            structured_logger.error("Error extrayendo email del remitente", {"error": str(e), "remitente": remitente})
            return None

    def construir_mensaje_raw_youchat(self, destinatario, msg_id_original=None, youchat_profile_headers=None, asunto_original=None):
        """Construye el mensaje en formato RAW optimizado para YouChat"""
        try:
            structured_logger.info("Construyendo mensaje RAW", {"destinatario": destinatario})
            headers_string = ""
            reserved_headers = [
                'Message-ID', 'Msg_id', 'In-Reply-To', 'References', 'Chat-Version', 'Pd',
                'MIME-Version', 'Content-Type', 'Content-Transfer-Encoding', 'From', 'To', 'Subject', 'Date',
                'Sender-Alias', 'From-Alias'
            ]
            if youchat_profile_headers and isinstance(youchat_profile_headers, dict):
                for key, value in youchat_profile_headers.items():
                    if value and key not in reserved_headers:
                        safe_value = str(value).replace('\r', '').replace('\n', '')[:998]
                        if all(32 <= ord(c) <= 126 for c in safe_value):
                            headers_string += f"{key}: {safe_value}\r\n"
                        else:
                            structured_logger.warning(f"Header {key} ignorado por caracteres inválidos", {"value": safe_value})

            domain = EMAIL_ACCOUNT.split('@')[1]
            nuevo_msg_id = f"<auto-reply-{uuid.uuid4()}@{domain}>"
            headers_string += f"Message-ID: {nuevo_msg_id}\r\n"

            if msg_id_original:
                clean_message_id = msg_id_original
                if not (msg_id_original.startswith('<') and msg_id_original.endswith('>')):
                    clean_message_id = f"<{msg_id_original}>"
                headers_string += f"In-Reply-To: {clean_message_id}\r\n"
                headers_string += f"References: {clean_message_id}\r\n"

            headers_string += f"Msg_id: auto-reply-{uuid.uuid4()}\r\n"

            chat_version = youchat_profile_headers.get('Chat-Version', '1.1') if youchat_profile_headers else '1.1'
            headers_string += f"Chat-Version: {chat_version}\r\n"

            pd_value = youchat_profile_headers.get('Pd') if youchat_profile_headers else None
            if pd_value:
                safe_pd = str(pd_value).strip()[:998]
                if all(32 <= ord(c) <= 126 for c in safe_pd):
                    headers_string += f"Pd: {safe_pd}\r\n"

            headers_string += f"Date: {formatdate(time.time(), localtime=True)}\r\n"
            headers_string += "MIME-Version: 1.0\r\n"
            headers_string += 'Content-Type: text/plain; charset="UTF-8"\r\n'
            headers_string += 'Content-Transfer-Encoding: 8bit\r\n'

            asunto = "YouChat"
            if asunto_original:
                if not asunto_original.lower().startswith('re:'):
                    asunto = f"Re: {asunto_original}"
                else:
                    asunto = asunto_original

            mensaje_texto = "¡Hola! Soy un bot en desarrollo. Pronto podré descargar tus Reels de Instagram."

            mail_raw = (
                f"From: {EMAIL_ACCOUNT}\r\n" +
                f"To: {destinatario}\r\n" +
                f"Subject: {asunto}\r\n" +
                headers_string +
                f"\r\n" +
                f"{mensaje_texto}"
            )

            structured_logger.info("Mensaje RAW construido exitosamente", {"destinatario": destinatario})
            return mail_raw.encode('utf-8')
        except Exception as e:
            structured_logger.error("Error construyendo mensaje RAW", {"error": str(e), "traceback": traceback.format_exc()})
            return None

    def enviar_respuesta_raw(self, destinatario, msg_id_original=None, youchat_profile_headers=None, asunto_original=None):
        """Envía respuesta usando formato RAW con reintentos"""
        try:
            structured_logger.info("Iniciando envío de respuesta RAW", {"destinatario": destinatario})
            mensaje_raw = self.construir_mensaje_raw_youchat(
                destinatario, msg_id_original, youchat_profile_headers, asunto_original
            )
            if not mensaje_raw:
                structured_logger.error("No se pudo construir el mensaje RAW")
                return False

            self.reset_email_count()
            if self.emails_sent_today >= 100:
                structured_logger.warning("Límite de emails alcanzado, esperando")
                return False

            retries = 3
            for attempt in range(retries):
                try:
                    structured_logger.info(f"Conectando al servidor SMTP (intento {attempt + 1}/{retries})")
                    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as servidor:
                        structured_logger.info("Iniciando TLS")
                        servidor.starttls()
                        structured_logger.info("Autenticando con Gmail")
                        servidor.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
                        structured_logger.info("Enviando email")
                        servidor.sendmail(EMAIL_ACCOUNT, destinatario, mensaje_raw)
                    structured_logger.info("Respuesta RAW enviada exitosamente", {"destinatario": destinatario})
                    self.emails_sent_today += 1
                    return True
                except (smtplib.SMTPException, OSError) as e:
                    structured_logger.error(f"Error en intento {attempt + 1}", {"error": str(e)})
                    if attempt < retries - 1:
                        sleep_time = 2 ** attempt
                        structured_logger.info(f"Reintentando en {sleep_time} segundos")
                        time.sleep(sleep_time)
                    else:
                        structured_logger.error("Falló tras todos los reintentos")
                        return False
        except Exception as e:
            structured_logger.error("Error inesperado enviando respuesta", {"error": str(e), "traceback": traceback.format_exc()})
            return False

    def procesar_emails_no_leidos(self):
        """Procesa emails no leídos con manejo robusto"""
        mail = self.verificar_conexion_imap()
        if not mail:
            structured_logger.error("No se pudo establecer conexión IMAP")
            return

        structured_logger.info("Buscando emails no leídos")
        estado, mensajes = mail.search(None, "UNSEEN")
        if estado != "OK":
            structured_logger.info("No hay emails nuevos o error en búsqueda")
            return

        ids_emails = mensajes[0].split()
        if not ids_emails:
            structured_logger.info("Cero emails no leídos encontrados")
            return

        structured_logger.info(f"{len(ids_emails)} nuevo(s) email(s) para procesar")
        for id_email in ids_emails:
            try:
                email_id = id_email.decode()
                if email_id in self.processed_emails:
                    structured_logger.info("Email ya procesado", {"email_id": email_id})
                    continue

                structured_logger.info("Procesando email", {"email_id": email_id})
                estado, datos_msg = mail.fetch(id_email, "(RFC822)")
                if estado != "OK" or not datos_msg:
                    structured_logger.error("Error obteniendo email o datos vacíos", {"email_id": email_id})
                    continue

                email_crudo = None
                if isinstance(datos_msg[0], tuple) and len(datos_msg[0]) >= 2:
                    email_crudo = datos_msg[0][1]
                    structured_logger.info("Datos obtenidos de posición [0][1]", {"email_id": email_id})
                elif isinstance(datos_msg[0], bytes):
                    email_crudo = datos_msg[0]
                    structured_logger.info("Datos obtenidos de posición [0]", {"email_id": email_id})
                else:
                    for i, item in enumerate(datos_msg):
                        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                            email_crudo = item[1]
                            structured_logger.info(f"Datos obtenidos de posición [{i}][1]", {"email_id": email_id})
                            break
                        elif isinstance(item, bytes):
                            email_crudo = item
                            structured_logger.info(f"Datos obtenidos de posición [{i}]", {"email_id": email_id})
                            break

                if not email_crudo:
                    structured_logger.error("No se pudieron extraer datos del email", {"email_id": email_id})
                    continue

                structured_logger.info(f"Email crudo obtenido correctamente ({len(email_crudo)} bytes)")
                mensaje = email.message_from_bytes(email_crudo)
                remitente = mensaje["From"]
                asunto_original = mensaje.get("Subject", "")
                email_remitente = self.extraer_email_remitente(remitente)

                if not email_remitente:
                    structured_logger.error("No se pudo extraer email del remitente", {"remitente": remitente})
                    continue

                structured_logger.info("Procesando mensaje", {"remitente": email_remitente, "asunto": asunto_original})
                headers_youchat = self.extraer_headers_youchat(mensaje)
                msg_id_original = mensaje.get('Message-ID') or headers_youchat.get('Message-ID')

                if msg_id_original:
                    structured_logger.info("Message-ID del mensaje original", {"msg_id": msg_id_original})

                exito = self.enviar_respuesta_raw(
                    email_remitente,
                    msg_id_original=msg_id_original,
                    youchat_profile_headers=headers_youchat,
                    asunto_original=asunto_original
                )

                if exito:
                    self.processed_emails.add(email_id)
                    self.total_processed += 1
                    structured_logger.info(f"Respuesta #{self.total_processed} enviada exitosamente", {"remitente": email_remitente})
                else:
                    structured_logger.error("Falló el envío de la respuesta", {"remitente": email_remitente})

            except Exception as e:
                structured_logger.error("Error procesando email", {"email_id": email_id, "error": str(e), "traceback": traceback.format_exc()})

    def limpiar_emails_procesados(self, max_age_hours=24):
        """Limpia emails procesados para evitar consumo excesivo de memoria"""
        if len(self.processed_emails) > 1000:
            structured_logger.info("Limpiando emails procesados antiguos")
            self.processed_emails.clear()

    def run_bot(self):
        """Ejecuta el bot en un bucle continuo"""
        self.is_running = True
        # LINEA 393 CORREGIDA
        structured_logger.info("Bot YouChat INICIADO - VERSIÓN CON CONEXIÓN ROBUSTA", {"interval": CHECK_INTERVAL, "email_account": EMAIL_ACCOUNT})
        consecutive_errors = 0
        max_consecutive_errors = 5

        while self.is_running:
            try:
                self.last_check = datetime.now()
                structured_logger.info("Revisando nuevos emails", {"timestamp": self.last_check.strftime('%H:%M:%S')})
                self.procesar_emails_no_leidos()
                self.limpiar_emails_procesados()
                consecutive_errors = 0
                time.sleep(CHECK_INTERVAL)
            except KeyboardInterrupt:
                structured_logger.info("Interrupción detectada, deteniendo bot")
                self.is_running = False
                break
            except Exception as e:
                consecutive_errors += 1
                structured_logger.error(f"Error #{consecutive_errors} en el bucle principal", {"error": str(e), "traceback": traceback.format_exc()})
                if consecutive_errors >= max_consecutive_errors:
                    structured_logger.error("Demasiados errores consecutivos, reiniciando conexiones")
                    self.cerrar_conexion_imap()
                    consecutive_errors = 0
                    time.sleep(10)
                else:
                    time.sleep(CHECK_INTERVAL)
        self.cerrar_conexion_imap()
        structured_logger.info("Bot YouChat detenido")

# =============================================================================
# INSTANCIA GLOBAL DEL BOT
# =============================================================================
youchat_bot = YouChatBot()
bot_thread = None

# =============================================================================
# RUTAS DEL SERVICIO WEB
# =============================================================================
@app.route('/')
def home():
    # LINEA 445 CORREGIDA
    return jsonify({
        "status": "online",
        "service": "YouChat Bot - Conexión Robusta",
        "version": "2.4",
        "features": [
            "Conexión IMAP persistente",
            "Reconexión automática",
            "Manejo robusto de errores",
            "Logging estructurado y rotación",
            "Reintentos SMTP",
            "Headers optimizados"
        ],
        "interval": f"{CHECK_INTERVAL} segundos",
        "email_account": EMAIL_ACCOUNT,
        "last_check": youchat_bot.last_check.isoformat() if youchat_bot.last_check else None,
        "total_processed": youchat_bot.total_processed,
        "emails_sent_today": youchat_bot.emails_sent_today,
        "is_running": youchat_bot.is_running,
        "imap_connected": youchat_bot.imap_connection is not None
    })

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "bot_running": youchat_bot.is_running,
        "imap_connected": youchat_bot.imap_connection is not None,
        "smtp_reachable": youchat_bot.check_smtp_health(),
        "memory_usage": f"{len(youchat_bot.processed_emails)} emails procesados"
    })

@app.route('/smtp_health')
def smtp_health():
    return jsonify({"smtp_reachable": youchat_bot.check_smtp_health()})

@app.route('/start')
def start_bot():
    global bot_thread
    if youchat_bot.is_running:
        return jsonify({
            "status": "already_running",
            "message": "El bot ya está en ejecución",
            "total_processed": youchat_bot.total_processed
        })
    youchat_bot.is_running = True
    bot_thread = threading.Thread(target=youchat_bot.run_bot, daemon=True)
    bot_thread.start()
    structured_logger.info("Bot iniciado correctamente")
    return jsonify({
        "status": "started",
        "message": "Bot iniciado correctamente",
        "features": "Conexión robusta IMAP y SMTP activada"
    })

@app.route('/stop')
def stop_bot():
    youchat_bot.is_running = False
    youchat_bot.cerrar_conexion_imap()
    structured_logger.info("Bot detenido")
    return jsonify({
        "status": "stopped",
        "message": "Bot detenido",
        "final_stats": {
            "total_processed": youchat_bot.total_processed,
            "last_check": youchat_bot.last_check.isoformat() if youchat_bot.last_check else None
        }
    })

@app.route('/status')
def status():
    # LINEA 508 CORREGIDA
    return jsonify({
        "is_running": youchat_bot.is_running,
        "last_check": youchat_bot.last_check.isoformat() if youchat_bot.last_check else None,
        "total_processed": youchat_bot.total_processed,
        "emails_sent_today": youchat_bot.emails_sent_today,
        "check_interval": CHECK_INTERVAL,
        "processed_emails_count": len(youchat_bot.processed_emails),
        "imap_connected": youchat_bot.imap_connection is not None,
        "smtp_reachable": youchat_bot.check_smtp_health()
    })

# =============================================================================
# INICIALIZACIÓN AUTOMÁTICA
# =============================================================================
def inicializar_bot():
    """Inicializa el bot automáticamente al cargar la aplicación"""
    global bot_thread
    structured_logger.info("Iniciando bot automáticamente", {
        "version": "2.4",
        "features": [
            "Conexión IMAP persistente",
            "Reconexión automática",
            "Manejo robusto de errores",
            "Logging estructurado",
            "Reintentos SMTP",
            "Headers optimizados"
        ]
    })
    youchat_bot.is_running = True
    bot_thread = threading.Thread(target=youchat_bot.run_bot, daemon=True)
    bot_thread.start()
    structured_logger.info("Bot iniciado y listo para recibir mensajes")

# Iniciar el bot cuando se carga la aplicación
inicializar_bot()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    structured_logger.info("Iniciando servidor web", {"port": port})
    app.run(host='0.0.0.0', port=port, debug=False)

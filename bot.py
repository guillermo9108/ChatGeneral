import os
import time
import imaplib
import email
from flask import Flask, jsonify
import threading
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import traceback
import sys
import json
from email.utils import parseaddr, formatdate
import uuid
from typing import Optional, Dict, Any, List

# Configuración de logging mejorada con rotación
logging.basicConfig(
    level=logging.INFO,  # INFO para un entorno de producción, DEBUG para desarrollo
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler('youchat_bot.log', maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Clase para logging estructurado
class StructuredLogger:
    """Clase simple para envolver el logger y permitir datos estructurados."""
    def __init__(self, logger):
        self.logger = logger

    def info(self, message: str, extra: Optional[Dict[str, Any]] = None):
        log_data = {"message": message, **(extra or {})}
        self.logger.info(json.dumps(log_data))

    def error(self, message: str, extra: Optional[Dict[str, Any]] = None):
        log_data = {"message": message, **(extra or {})}
        self.logger.error(json.dumps(log_data))

structured_logger = StructuredLogger(logger)

app = Flask(__name__)

# =============================================================================
# CONFIGURACIÓN (Solo IMAP)
# =============================================================================
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_USERNAME = os.getenv("IMAP_USERNAME")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD")
EMAIL_FOLDER = "INBOX"

CHECK_INTERVAL = 5  # Segundos entre chequeos

# =============================================================================
# CLASE PRINCIPAL DEL BOT (Solo para recibir correos)
# =============================================================================

class YouChatBot:
    """Clase central para manejar la conexión IMAP y el procesamiento de correos."""
    def __init__(self):
        self.is_running = False
        self.imap_connection: Optional[imaplib.IMAP4_SSL] = None
        self.processed_emails: List[str] = [] # Almacena Message-IDs para evitar duplicados
        self.total_processed = 0
        self.last_check: Optional[datetime] = None

    def connect_imap(self) -> bool:
        """Intenta conectar y logearse al servidor IMAP."""
        if self.imap_connection:
            try:
                self.imap_connection.noop()
                return True # Conexión existente y activa
            except:
                structured_logger.info("Conexión IMAP inactiva. Reintentando conectar...")
                self.close_imap()
        
        if not IMAP_USERNAME or not IMAP_PASSWORD:
            structured_logger.error("Credenciales IMAP no configuradas (IMAP_USERNAME/IMAP_PASSWORD)")
            return False

        try:
            self.imap_connection = imaplib.IMAP4_SSL(IMAP_SERVER)
            self.imap_connection.login(IMAP_USERNAME, IMAP_PASSWORD)
            self.imap_connection.select(EMAIL_FOLDER)
            structured_logger.info("Conexión IMAP exitosa y carpeta seleccionada", {"server": IMAP_SERVER, "folder": EMAIL_FOLDER})
            return True
        except imaplib.IMAP4.error as e:
            structured_logger.error("Error al conectar o logear a IMAP", {"error": str(e)})
            self.imap_connection = None
            return False
        except Exception as e:
            structured_logger.error("Error desconocido en la conexión IMAP", {"error": str(e), "traceback": traceback.format_exc()})
            self.imap_connection = None
            return False

    def close_imap(self):
        """Cierra la conexión IMAP."""
        if self.imap_connection:
            try:
                self.imap_connection.logout()
                structured_logger.info("Conexión IMAP cerrada.")
            except Exception as e:
                structured_logger.error("Error al cerrar conexión IMAP", {"error": str(e)})
            self.imap_connection = None

    def fetch_new_emails(self) -> List[Dict[str, str]]:
        """Busca y descarga correos no procesados."""
        new_emails_data = []
        if not self.connect_imap() or not self.imap_connection:
            return new_emails_data

        try:
            # Buscar correos no leídos
            status, email_ids = self.imap_connection.search(None, 'UNSEEN')
            if status != 'OK':
                structured_logger.info("No se encontraron correos nuevos.")
                return new_emails_data

            id_list = email_ids[0].split()
            structured_logger.info(f"Correos encontrados: {len(id_list)}", {"count": len(id_list)})

            for email_id in id_list:
                status, msg_data = self.imap_connection.fetch(email_id, '(RFC822)')
                if status != 'OK':
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                message_id = msg.get('Message-ID', f"No-ID-{uuid.uuid4()}")

                if message_id in self.processed_emails:
                    continue
                
                # Intentar marcar como leído para evitar procesarlo de nuevo en la próxima ejecución
                # Aún si falla la marca, el Message-ID lo protege.
                try:
                    self.imap_connection.store(email_id, '+FLAGS', '\\Seen')
                except Exception as e:
                    structured_logger.error("Fallo al marcar el correo como leído", {"id": email_id, "error": str(e)})

                # Extraer cuerpo del correo (solo texto plano)
                body = self._get_email_body(msg)
                
                # Construir el objeto de correo
                sender_name, sender_email = parseaddr(msg.get('From'))
                subject = msg.get('Subject', 'Sin Asunto')
                
                new_email_data = {
                    "id": message_id,
                    "from_name": sender_name,
                    "from_email": sender_email,
                    "subject": subject,
                    "body_snippet": body[:200] + "..." if len(body) > 200 else body,
                    "date": formatdate(datetime.now().timestamp(), localtime=True)
                }
                
                new_emails_data.append(new_email_data)
                self.processed_emails.append(message_id)
                self.total_processed += 1

            self.imap_connection.expunge() # Elimina mensajes marcados para borrado (si los hubiera)
        except Exception as e:
            structured_logger.error("Error al procesar correos", {"error": str(e), "traceback": traceback.format_exc()})
            self.close_imap() # Forzar reconexión
            
        return new_emails_data

    def _get_email_body(self, msg: email.message.Message) -> str:
        """Extrae el cuerpo de texto plano del objeto de correo."""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                cdispo = str(part.get('Content-Disposition'))

                # Buscar la parte de texto plano, ignorando archivos adjuntos
                if ctype == 'text/plain' and 'attachment' not in cdispo:
                    try:
                        return part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='ignore')
                    except:
                        return "Error al decodificar el cuerpo del correo."
        else:
            try:
                return msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', errors='ignore')
            except:
                return "Error al decodificar el cuerpo del correo."
        return "Cuerpo de correo no disponible (solo HTML o adjuntos)."

    def run_bot(self):
        """Bucle principal de ejecución del bot."""
        structured_logger.info("Bot en funcionamiento...")
        while self.is_running:
            try:
                # 1. Chequear y procesar correos nuevos
                new_emails = self.fetch_new_emails()
                if new_emails:
                    structured_logger.info(f"Nuevos correos procesados: {len(new_emails)}", {"new_count": len(new_emails)})
                    # Aquí es donde se podría implementar una cola para pasar los datos al frontend.
                    # Por ahora, solo se registran, y el endpoint /inbox los expone.
                
                self.last_check = datetime.now()
                
            except Exception as e:
                structured_logger.error("Fallo inesperado en el bucle principal del bot", {"error": str(e), "traceback": traceback.format_exc()})
                self.close_imap()

            time.sleep(CHECK_INTERVAL)

# =============================================================================
# INICIALIZACIÓN Y ENDPOINTS WEB
# =============================================================================
youchat_bot = YouChatBot()

@app.route('/status', methods=['GET'])
def get_status():
    """Retorna el estado operativo del bot (solo IMAP)."""
    return jsonify({
        "is_running": youchat_bot.is_running,
        "last_check": youchat_bot.last_check.strftime("%Y-%m-%d %H:%M:%S") if youchat_bot.last_check else None,
        "total_emails_processed": youchat_bot.total_processed,
        "check_interval_seconds": CHECK_INTERVAL,
        "unique_emails_tracked": len(youchat_bot.processed_emails),
        "imap_connected": youchat_bot.imap_connection is not None,
    })

@app.route('/inbox', methods=['GET'])
def get_inbox():
    """Retorna la lista de correos procesados (para simular el frontend)."""
    # En una aplicación real, probablemente harías una nueva búsqueda o devolverías
    # la cola de nuevos correos. Aquí, devolvemos la lista de IDs procesados.
    return jsonify({
        "processed_ids": youchat_bot.processed_emails,
        "total_count": youchat_bot.total_processed,
        "message": "Lista de IDs de correos procesados."
    })


def inicializar_bot():
    """Inicializa el bot automáticamente al cargar la aplicación"""
    global bot_thread
    structured_logger.info("Iniciando bot IMAP (sin SMTP)...", {
        "version": "2.5 (IMAP Only)",
        "features": [
            "Conexión IMAP persistente",
            "Reconexión automática",
            "Manejo robusto de errores",
            "Logging estructurado",
            "Chequeo de nuevos correos"
        ]
    })
    youchat_bot.is_running = True
    bot_thread = threading.Thread(target=youchat_bot.run_bot, daemon=True)
    bot_thread.start()
    structured_logger.info("Bot iniciado y listo.")

# Iniciar el bot cuando se carga la aplicación
inicializar_bot()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    # Aquí se utiliza '0.0.0.0' para que sea accesible externamente en el entorno de Render/Canvas
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)

# Manejo de apagado del bot para cerrar la conexión IMAP
def signal_handler(sig, frame):
    structured_logger.info("Señal de apagado recibida. Deteniendo bot...")
    youchat_bot.is_running = False
    if youchat_bot.imap_connection:
        youchat_bot.close_imap()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

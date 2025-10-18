import os
from server import init_db, app
from server import db

# Necesitas ejecutar la inicialización de la base de datos
# dentro del contexto de la aplicación de Flask
with app.app_context():
    # Inicializa las tablas si no existen
    init_db()
    # Confirma cualquier cambio pendiente
    db.session.commit()
    print("Base de datos inicializada o verificada.")

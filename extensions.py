"""
Instâncias compartilhadas do SQLAlchemy e do Flask-Login.

Ficam separadas do app.py para evitar import circular: models.py precisa
de `db`, e app.py precisa tanto de `db` quanto dos models.
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message_category = "warning"

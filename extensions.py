from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from flask_login import LoginManager

db = SQLAlchemy()
socketio = SocketIO(cors_allowed_origins="*") 
login_manager = LoginManager()

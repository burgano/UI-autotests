from flask import Flask
from flask_socketio import SocketIO

socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")


def create_app():
    app = Flask(__name__)
    app.secret_key = "ui-autotest-generator-secret"

    socketio.init_app(app)

    from app.routes import bp
    app.register_blueprint(bp)

    return app

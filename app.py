import os
from flask import Flask
from decimal import Decimal
from extensions import db, migrate

def create_app():
    """Application Factory."""
    app = Flask(__name__)
    basedir = os.path.abspath(os.path.dirname(__file__))

    # --- Configuration ---
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'a-very-secret-key'
    app.config['SQLALCHEMY_DATABASE_URI'] = (
        os.environ.get('DATABASE_URL') or
        'sqlite:///' + os.path.join(basedir, 'app.db')
    )
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'uploads')
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    app.config['ITEMS_PER_PAGE'] = 20

    # --- Initialize Extensions ---
    db.init_app(app)
    migrate.init_app(app, db)

    # --- Register Jinja Filters ---
    @app.template_filter()
    def trim_zeros(value):
        if isinstance(value, Decimal):
            value = value.normalize().to_eng_string()
        if isinstance(value, str) and '.' in value:
            return value.rstrip('0').rstrip('.')
        return value

    # --- Register Blueprints ---
    with app.app_context():
        # Import blueprints inside the context
        from main_routes import main_bp
        from commands import analytics_cli
        from securities_logic import securities_bp

        app.register_blueprint(main_bp)
        app.register_blueprint(securities_bp)
        app.cli.add_command(analytics_cli)

    return app

if __name__ == '__main__':
    app = create_app()
    app.run(debug=True, host='0.0.0.0')

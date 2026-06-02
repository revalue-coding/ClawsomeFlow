"""Allow ``python -m app.cli`` as an alias for ``csflow``."""
from app.cli import app

if __name__ == "__main__":
    app()

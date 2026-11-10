import os

from kova import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("KOVA_HOST", "127.0.0.1"),
            port=int(os.environ.get("KOVA_PORT", "5000")),
            debug=app.config["ENV_NAME"] == "development")

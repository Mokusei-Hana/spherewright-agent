"""CLI entry point for the Spherewright Agent Web Console server."""

import uvicorn

from .app import create_app


def serve(config_path: str = "config.toml", host: str = "0.0.0.0", port: int = 8000):
    """Start the Web Console server."""
    app = create_app(config_path)
    print(f"Starting Spherewright Agent Console at http://{host}:{port}")
    print(f"Config: {config_path}")
    print("Press Ctrl+C to stop.")
    uvicorn.run(app, host=host, port=port)

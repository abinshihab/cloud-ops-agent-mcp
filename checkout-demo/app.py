import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

RELEASE = os.getenv("APP_RELEASE", "v1")
MODE = os.getenv("APP_MODE", "healthy")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        request_id = str(uuid4())
        status = 200
        error_code = None

        if path == "/health":
            body = {"status": "healthy", "release": RELEASE}
        elif path == "/checkout":
            if MODE == "faulty":
                status = 500
                error_code = "CHECKOUT_CONFIG_ERROR"
                body = {"error": "Checkout unavailable"}
            else:
                body = {"status": "success", "order_id": str(uuid4())}
        else:
            status = 404
            body = {"error": "Not found"}

        body.update(release=RELEASE, request_id=request_id)

        print(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "checkout-api",
            "release": RELEASE,
            "level": "ERROR" if status >= 500 else "INFO",
            "path": path,
            "status_code": status,
            "request_id": request_id,
            "error_code": error_code,
            "message": (
                "Checkout configuration invalid: unsupported pricing rule"
                if error_code else "Request completed"
            ),
        }), flush=True)

        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

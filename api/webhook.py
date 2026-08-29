from http.server import BaseHTTPRequestHandler
import json

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"statusCode": 200, "body": "OK - GET"}).encode())
        return

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            # Always return 200 regardless of body
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"statusCode": 200, "body": "OK - POST"}).encode())
        except Exception:
            try:
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"statusCode":200,"body":"OK"}')
            except Exception:
                pass
        return

    def log_message(self, format, *args):
        return

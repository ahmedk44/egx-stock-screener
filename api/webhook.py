def handler(request, *args, **kwargs):
    return {"statusCode": 200, "body": "OK - minimal py"}

def app(request, *args, **kwargs):
    return handler(request, *args, **kwargs)

# Vercel Python class handler fallback
try:
    from http.server import BaseHTTPRequestHandler
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def do_POST(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format, *args):
            pass
    handler_class = Handler
except Exception:
    pass

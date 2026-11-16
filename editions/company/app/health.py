"""C02 transport placeholder. Later phases replace this with the Company app."""

from http.server import BaseHTTPRequestHandler, HTTPServer


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/company/health":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, _format, *_args):
        pass


HTTPServer(("127.0.0.1", 8765), Health).serve_forever()

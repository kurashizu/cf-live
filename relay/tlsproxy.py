#!/usr/bin/env python3
"""Terminate TLS in front of the relay's plain-HTTP port.

The relay binds 127.0.0.1 and speaks HTTP. Players that refuse cleartext
need an https:// URL, so this wraps the same content in TLS without
touching recv.py: run it alongside, point it at the relay's port, and the
relay keeps working exactly as before if this process dies.

A self-signed certificate is enough for curl -k, VLC and a browser you are
willing to click through. It is NOT enough for VRChat: AVPro is
MediaFoundation on Windows and ExoPlayer on Quest, both of which validate
against the OS trust store and expose no way to accept an unknown issuer.
For those, the certificate has to chain to a public CA, which in practice
means a hostname rather than a bare IP.

  python3 tlsproxy.py --listen 0.0.0.0:9999 --origin 127.0.0.1:9990 \
      --cert /opt/krsz-relay/tls/cert.pem --key /opt/krsz-relay/tls/key.pem

Python 3.6 compatible: the China box runs 3.6.8.
"""
import argparse
import socket
import socketserver
import ssl
import sys
import threading

try:
    from http.client import HTTPConnection
except ImportError:  # pragma: no cover
    from httplib import HTTPConnection

# Hop-by-hop headers must not be forwarded: they describe this connection,
# not the message, and echoing them corrupts framing on the client side.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


class Proxy(socketserver.StreamRequestHandler):
    # Segments are small and the relay is on loopback; a short timeout keeps
    # a stalled client from pinning a thread.
    timeout = 20

    def handle(self):
        try:
            line = self.rfile.readline(65536)
        except (socket.timeout, OSError):
            return
        if not line:
            return
        try:
            method, path, _version = line.decode("latin-1").split()
        except ValueError:
            return

        headers = {}
        while True:
            h = self.rfile.readline(65536)
            if not h or h in (b"\r\n", b"\n"):
                break
            try:
                k, v = h.decode("latin-1").split(":", 1)
            except ValueError:
                continue
            headers[k.strip()] = v.strip()

        body = b""
        n = headers.get("Content-Length")
        if n and n.isdigit():
            body = self.rfile.read(int(n))

        fwd = {k: v for k, v in headers.items()
               if k.lower() not in HOP_BY_HOP and k.lower() != "host"}
        fwd["Host"] = self.server.origin_host

        try:
            conn = HTTPConnection(self.server.origin_host,
                                  self.server.origin_port, timeout=15)
            conn.request(method, path, body=body or None, headers=fwd)
            res = conn.getresponse()
            payload = res.read()
        except Exception as exc:
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n"
                             b"Content-Length: 12\r\n"
                             b"Connection: close\r\n\r\nbad gateway\n")
            sys.stderr.write("upstream error: %r\n" % (exc,))
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

        out = ["HTTP/1.1 %d %s" % (res.status, res.reason)]
        for k, v in res.getheaders():
            if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                continue
            out.append("%s: %s" % (k, v))
        # Re-derive the length from what was actually read, and close each
        # response: the relay's playlists are no-store anyway, and keeping
        # this proxy connectionless keeps it simple to reason about.
        # For HEAD the body is empty but the length must still describe the
        # entity, or a client sizing a request from it reads zero.
        if method == "HEAD":
            upstream_len = res.getheader("Content-Length")
            out.append("Content-Length: %s" % (upstream_len or len(payload)))
        else:
            out.append("Content-Length: %d" % len(payload))
        out.append("Connection: close")
        head = ("\r\n".join(out) + "\r\n\r\n").encode("latin-1")
        try:
            self.wfile.write(head)
            if method != "HEAD":
                self.wfile.write(payload)
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    # ffmpeg opens several connections at once and abandons some of them.
    # The default of 5 filled up and new clients could not even complete a
    # TCP connect.
    request_queue_size = 128

    def get_request(self):
        """Accept in the clear, hand the handshake to the worker thread.

        Wrapping the listening socket instead performs the TLS handshake
        inside accept() on the main thread, so a single slow or abandoned
        client blocks every other connection -- which is exactly how this
        wedged: listen queue full, one thread alive, TCP connects timing out.
        """
        sock, addr = self.socket.accept()
        return sock, addr

    def process_request_thread(self, request, client_address):
        try:
            # Deadline on the handshake itself; an idle TCP connection that
            # never sends a ClientHello must not hold a thread forever.
            request.settimeout(15)
            tls = self.tls_context.wrap_socket(request, server_side=True)
        except Exception:
            self.shutdown_request(request)
            return
        try:
            self.finish_request(tls, client_address)
        except Exception:
            pass
        finally:
            self.shutdown_request(tls)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="0.0.0.0:9999")
    ap.add_argument("--origin", default="127.0.0.1:9990")
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    args = ap.parse_args()

    lh, lp = args.listen.rsplit(":", 1)
    oh, op = args.origin.rsplit(":", 1)

    srv = Server((lh, int(lp)), Proxy)
    srv.origin_host, srv.origin_port = oh, int(op)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(args.cert, args.key)
    # Held for the worker threads to use; the listening socket stays plain.
    srv.tls_context = ctx

    print("tls on %s -> http://%s" % (args.listen, args.origin), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

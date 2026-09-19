#!/usr/bin/env python3
"""Obtain a Let's Encrypt certificate over ACME http-01, with no dependencies.

certbot and acme.sh are not installed on the relay host and this project
does not install software there, so this implements the subset of RFC 8555
needed for one domain: new account, new order, http-01 challenge, finalize,
download. Keys and CSR are produced by shelling out to openssl, which is
present.

  python3 acme.py --domain cn.live.krsz.in \\
      --webroot /var/www/acme --out /opt/krsz-relay/tls

Writes cert.pem (leaf + intermediates) and key.pem into --out. Re-running
renews: ACME issues a fresh certificate each time, so this doubles as the
renewal path from cron.

Python 3.6 compatible.
"""
import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
UA = "krsz-live-acme/1.0"


def b64(b):
    """base64url without padding, which is what JWS uses throughout."""
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def run(cmd, stdin=None):
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    out, err = p.communicate(stdin)
    if p.returncode:
        raise RuntimeError("%s failed: %s" % (" ".join(cmd), err.decode()[:400]))
    return out


class Acme:
    def __init__(self, account_key, directory=DIRECTORY):
        self.key = account_key
        self.dir = json.loads(self._get(directory))
        self.nonce = None
        self.kid = None
        self._jwk_cache = None

    # ---------------------------------------------------------------- http
    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        return urllib.request.urlopen(req, timeout=30).read()

    def _head_nonce(self):
        req = urllib.request.Request(self.dir["newNonce"], method="HEAD",
                                     headers={"User-Agent": UA})
        r = urllib.request.urlopen(req, timeout=30)
        return r.headers["Replay-Nonce"]

    # ----------------------------------------------------------------- jws
    def jwk(self):
        """Public JWK for the RSA account key, read out of openssl's text."""
        if self._jwk_cache:
            return self._jwk_cache
        txt = run(["openssl", "rsa", "-in", self.key, "-noout",
                   "-text"]).decode("utf8", "replace")
        pub = re.search(r"publicExponent: (\d+)", txt).group(1)
        mod = re.search(r"modulus:\n\s+00:([a-f0-9:\s]+?)\npublicExponent",
                        txt, re.S).group(1)
        mod = binascii.unhexlify(re.sub(r"[\s:]", "", mod))
        e = int(pub)
        e_bytes = e.to_bytes((e.bit_length() + 7) // 8, "big")
        self._jwk_cache = {"e": b64(e_bytes), "kty": "RSA", "n": b64(mod)}
        return self._jwk_cache

    def thumbprint(self):
        j = self.jwk()
        canon = json.dumps(j, sort_keys=True, separators=(",", ":")).encode()
        return b64(hashlib.sha256(canon).digest())

    def signed(self, url, payload):
        """One JWS request. payload=None means POST-as-GET."""
        if self.nonce is None:
            self.nonce = self._head_nonce()
        protected = {"url": url, "alg": "RS256", "nonce": self.nonce}
        if self.kid:
            protected["kid"] = self.kid
        else:
            protected["jwk"] = self.jwk()
        p64 = b64(json.dumps(protected).encode())
        y64 = "" if payload is None else b64(json.dumps(payload).encode())
        sig = run(["openssl", "dgst", "-sha256", "-sign", self.key],
                  ("%s.%s" % (p64, y64)).encode())
        body = json.dumps({"protected": p64, "payload": y64,
                           "signature": b64(sig)}).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/jose+json", "User-Agent": UA})
        try:
            r = urllib.request.urlopen(req, timeout=60)
            data, headers, code = r.read(), r.headers, r.getcode()
        except urllib.error.HTTPError as e:
            data, headers, code = e.read(), e.headers, e.getcode()
        self.nonce = headers.get("Replay-Nonce") or self._head_nonce()
        if code >= 400:
            raise RuntimeError("ACME %s -> %s %s" % (url, code, data.decode()[:500]))
        return data, headers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--webroot", required=True,
                    help="directory whose .well-known/acme-challenge/ is served")
    ap.add_argument("--out", required=True)
    ap.add_argument("--contact", default="")
    ap.add_argument("--directory", default=DIRECTORY)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    acct = os.path.join(args.out, "account.key")
    if not os.path.exists(acct):
        run(["openssl", "genrsa", "-out", acct, "2048"])
        os.chmod(acct, 0o600)

    a = Acme(acct, args.directory)

    reg = {"termsOfServiceAgreed": True}
    if args.contact:
        reg["contact"] = ["mailto:" + args.contact]
    _, h = a.signed(a.dir["newAccount"], reg)
    a.kid = h["Location"]
    print("account:", a.kid)

    data, h = a.signed(a.dir["newOrder"],
                       {"identifiers": [{"type": "dns", "value": args.domain}]})
    order = json.loads(data)
    # The order's own URL is only in the Location header, and it is the one
    # that must be polled after finalize.
    order_url = h["Location"]

    for auth_url in order["authorizations"]:
        data, _ = a.signed(auth_url, None)
        auth = json.loads(data)
        ch = [c for c in auth["challenges"] if c["type"] == "http-01"][0]
        token = ch["token"]
        keyauth = "%s.%s" % (token, a.thumbprint())

        d = os.path.join(args.webroot, ".well-known", "acme-challenge")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, token)
        with open(path, "w") as f:
            f.write(keyauth)
        os.chmod(path, 0o644)
        print("challenge staged:", path)

        a.signed(ch["url"], {})
        for _ in range(40):
            time.sleep(3)
            data, _ = a.signed(auth_url, None)
            st = json.loads(data)["status"]
            if st == "valid":
                print("authorization valid")
                break
            if st == "invalid":
                raise RuntimeError("challenge failed: %s" % data.decode()[:500])
        else:
            raise RuntimeError("timed out waiting for validation")
        try:
            os.remove(path)
        except OSError:
            pass

    key = os.path.join(args.out, "key.pem")
    if not os.path.exists(key):
        run(["openssl", "genrsa", "-out", key, "2048"])
        os.chmod(key, 0o600)
    csr = os.path.join(args.out, "csr.pem")
    run(["openssl", "req", "-new", "-key", key, "-out", csr, "-subj",
         "/CN=" + args.domain, "-addext", "subjectAltName=DNS:" + args.domain])
    der = run(["openssl", "req", "-in", csr, "-outform", "DER"])

    data, _ = a.signed(order["finalize"], {"csr": b64(der)})
    o = json.loads(data)
    for _ in range(30):
        if o.get("status") == "valid":
            break
        if o.get("status") == "invalid":
            raise RuntimeError("order invalid: %s" % json.dumps(o)[:400])
        time.sleep(3)
        data, _ = a.signed(order_url, None)
        o = json.loads(data)
    if o.get("status") != "valid":
        raise RuntimeError("order not valid: %s" % json.dumps(o)[:400])

    data, _ = a.signed(o["certificate"], None)
    out = os.path.join(args.out, "cert.pem")
    with open(out, "wb") as f:
        f.write(data)
    print("wrote", out, len(data), "bytes")


if __name__ == "__main__":
    main()

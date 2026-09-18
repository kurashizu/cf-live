"""Reed-Solomon erasure coding over GF(256), standard library only.

Written for a link where the loss is known: ~17-23% of datagrams, arriving
as isolated singles (measured run lengths 1:144 2:18 3:2 4:3, max 4). That
profile is what makes forward correction the right tool instead of
retransmission -- a retransmit costs a full 281 ms round trip, while parity
costs only bandwidth, which this link has to spare.

Erasure coding only, not error correction: UDP checksums mean a datagram
either arrives intact or not at all, so the decoder always knows *which*
shards are missing. That is a much easier problem than unknown errors, and
it needs only a linear solve rather than a syndrome search.

The generator matrix is a Vandermonde matrix reduced so its first k rows are
the identity, which makes the data shards themselves systematic: a receiver
that loses nothing does no decoding work at all.
"""

# GF(256) with the standard AES/RS primitive polynomial 0x11d.
_EXP = [0] * 512
_LOG = [0] * 256


def _init_tables():
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_tables()


def gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def gdiv(a, b):
    if b == 0:
        raise ZeroDivisionError("GF(256) division by zero")
    if a == 0:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


def ginv(a):
    if a == 0:
        raise ZeroDivisionError("GF(256) inverse of zero")
    return _EXP[(255 - _LOG[a]) % 255]


# Multiplication tables, built once: the inner loops below run per byte of
# every shard, and a table lookup is several times faster than the log/exp
# path in pure Python.
_MUL = [bytes(gmul(a, b) for b in range(256)) for a in range(256)]


def _vandermonde(rows, cols):
    """rows x cols Vandermonde matrix: m[i][j] = i^j in GF(256)."""
    return [[_EXP[(_LOG[i] * j) % 255] if i else (1 if j == 0 else 0)
             for j in range(cols)] for i in range(rows)]


def _mat_mul(a, b):
    n, m, p = len(a), len(b), len(b[0])
    out = [[0] * p for _ in range(n)]
    for i in range(n):
        ai = a[i]
        oi = out[i]
        for k in range(m):
            aik = ai[k]
            if aik == 0:
                continue
            tab = _MUL[aik]
            bk = b[k]
            for j in range(p):
                oi[j] ^= tab[bk[j]]
    return out


def _mat_invert(m):
    """Gauss-Jordan inverse of a square GF(256) matrix."""
    n = len(m)
    a = [list(row) + [1 if i == j else 0 for j in range(n)]
         for i, row in enumerate(m)]
    for col in range(n):
        pivot = next((r for r in range(col, n) if a[r][col]), None)
        if pivot is None:
            raise ValueError("matrix is singular")
        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]
        inv = ginv(a[col][col])
        tab = _MUL[inv]
        a[col] = [tab[v] for v in a[col]]
        for r in range(n):
            if r == col or a[r][col] == 0:
                continue
            f = a[r][col]
            tf = _MUL[f]
            ar, ac = a[r], a[col]
            for j in range(2 * n):
                ar[j] ^= tf[ac[j]]
    return [row[n:] for row in a]


class Codec(object):
    """RS(k, n): k data shards, n-k parity shards, all the same length."""

    def __init__(self, k, n):
        if not 0 < k < n <= 255:
            raise ValueError("need 0 < k < n <= 255")
        self.k = k
        self.n = n
        # Reduce the top k x k block to the identity so data shards pass
        # through unchanged and a lossless receiver skips decoding entirely.
        vm = _vandermonde(n, k)
        top = [row[:] for row in vm[:k]]
        self.matrix = _mat_mul(vm, _mat_invert(top))

    def encode(self, data_shards):
        """data_shards: k equal-length bytes objects -> (n-k) parity shards."""
        if len(data_shards) != self.k:
            raise ValueError("expected %d data shards" % self.k)
        size = len(data_shards[0])
        if any(len(s) != size for s in data_shards):
            raise ValueError("shards must be equal length")
        # The inner loop is the hot path: it runs per byte of every shard for
        # every parity row. Doing it in Python costs ~65 ms per 12 KB block on
        # a small VPS core, which is under the bitrate this link needs. So the
        # multiply becomes bytes.translate (a C-level 256-entry lookup) and the
        # accumulate becomes one big-integer XOR, which keeps both out of the
        # interpreter.
        parity = []
        for row in self.matrix[self.k:]:
            acc = 0
            for coeff, shard in zip(row, data_shards):
                if coeff == 0:
                    continue
                acc ^= int.from_bytes(shard.translate(_MUL[coeff]), "big")
            parity.append(acc.to_bytes(size, "big"))
        return parity

    def decode(self, shards):
        """shards: list of n entries, each bytes or None -> k data shards.

        Raises ValueError when fewer than k shards survived, which the caller
        must treat as an unrecoverable block rather than a crash: the whole
        point of the design is that this happens occasionally.
        """
        if len(shards) != self.n:
            raise ValueError("expected %d slots" % self.n)
        present = [i for i, s in enumerate(shards) if s is not None]
        if len(present) < self.k:
            raise ValueError("only %d of %d shards present" %
                             (len(present), self.k))
        # Already have every data shard: nothing to solve.
        if all(shards[i] is not None for i in range(self.k)):
            return [shards[i] for i in range(self.k)]
        use = present[:self.k]
        sub = [self.matrix[i] for i in use]
        inv = _mat_invert(sub)
        size = len(shards[use[0]])
        out = []
        for row in inv:
            acc = 0
            for coeff, idx in zip(row, use):
                if coeff == 0:
                    continue
                acc ^= int.from_bytes(
                    bytes(shards[idx]).translate(_MUL[coeff]), "big")
            out.append(acc.to_bytes(size, "big"))
        return out

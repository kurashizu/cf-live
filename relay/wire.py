"""Wire format for the cross-border relay.

One design constraint shapes everything here: the Alibaba security group
blocks unsolicited inbound UDP, but its state table lets return traffic
through once the China side has sent outbound. So the receiver is the
initiator -- it keeps poking the sender, and media flows back through the
pinhole it opened. That inverts the usual push model, and it is why the
sender learns the peer address from the first packet rather than from config.

Packets are self-describing and independent: every shard carries enough
header to be placed without the ones before it. On a link that drops 20% of
datagrams in isolated singles, any format needing a preceding packet to be
parseable would amplify each loss into a burst.
"""

import struct

MAGIC = b"KLR1"

# Packet types.
HELLO = 1      # receiver -> sender: open/refresh the pinhole
SHARD = 2      # sender -> receiver: one FEC shard of one block
MANIFEST = 3   # sender -> receiver: the playlist, small and sent whole
BYE = 4        # sender -> receiver: stream ended

# struct formats. Kept explicit rather than pickled: the two ends are
# deployed separately and may not be updated together, so the format has to
# be inspectable and versioned by MAGIC.
_HDR = struct.Struct("!4sBB")          # magic, version, type
VERSION = 1

# SHARD body: stream_id, segment seq, block index, total blocks in the
# segment, shard index, k, n, payload length of the *block* (to strip
# padding after decode), then bytes.
#
# The total is repeated in every shard rather than announced once. A single
# announcement would be a packet whose loss strands the whole segment, and on
# this link one in five packets is lost; six bytes of repetition is cheaper
# than that failure mode.
_SHARD = struct.Struct("!HIHHBBBI")

# MANIFEST body: stream_id, generation, total length, offset, then bytes.
# Split across datagrams because a playlist can exceed a safe MTU, and each
# piece must stand alone for the same reason shards do.
_MANIFEST = struct.Struct("!HIII")

# HELLO body: stream_id, receiver nonce (so the sender can tell a restarted
# receiver from a duplicate).
_HELLO = struct.Struct("!HI")

# 1200 keeps the whole datagram under a conservative path MTU (1280, the
# IPv6 minimum) once IP and UDP headers are added, so nothing fragments.
# A fragmented datagram is lost entirely if any fragment is lost, which
# would multiply the loss rate.
SHARD_PAYLOAD = 1200


def header(ptype):
    return _HDR.pack(MAGIC, VERSION, ptype)


def parse_header(buf):
    """Return (type, body_offset) or (None, 0) if this is not ours."""
    if len(buf) < _HDR.size:
        return None, 0
    magic, ver, ptype = _HDR.unpack_from(buf, 0)
    if magic != MAGIC or ver != VERSION:
        return None, 0
    return ptype, _HDR.size


def pack_shard(stream_id, seq, block, blocks, shard_idx, k, n, block_len,
               payload):
    return (header(SHARD)
            + _SHARD.pack(stream_id, seq, block, blocks, shard_idx, k, n,
                          block_len)
            + payload)


def unpack_shard(buf, off):
    (stream_id, seq, block, blocks, shard_idx, k, n,
     block_len) = _SHARD.unpack_from(buf, off)
    return {
        "stream_id": stream_id, "seq": seq, "block": block,
        "blocks": blocks, "shard": shard_idx, "k": k, "n": n,
        "block_len": block_len,
        "payload": buf[off + _SHARD.size:],
    }


def pack_manifest(stream_id, generation, total, offset, chunk):
    return (header(MANIFEST)
            + _MANIFEST.pack(stream_id, generation, total, offset) + chunk)


def unpack_manifest(buf, off):
    stream_id, generation, total, offset = _MANIFEST.unpack_from(buf, off)
    return {
        "stream_id": stream_id, "generation": generation,
        "total": total, "offset": offset,
        "chunk": buf[off + _MANIFEST.size:],
    }


def pack_hello(stream_id, nonce):
    return header(HELLO) + _HELLO.pack(stream_id, nonce)


def unpack_hello(buf, off):
    stream_id, nonce = _HELLO.unpack_from(buf, off)
    return {"stream_id": stream_id, "nonce": nonce}


def pack_bye(stream_id):
    return header(BYE) + struct.pack("!H", stream_id)


def unpack_bye(buf, off):
    return {"stream_id": struct.unpack_from("!H", buf, off)[0]}


def shards_for(data, k, shard_len=SHARD_PAYLOAD):
    """Split a blob into k equal shards, zero-padded.

    Returns (shards, padded_len). The caller sends the original length so the
    receiver can strip padding: without it a decoded block would carry
    trailing zeros into the media stream.
    """
    need = k * shard_len
    if len(data) > need:
        raise ValueError("data too large for %d x %d" % (k, shard_len))
    buf = data + b"\x00" * (need - len(data))
    return [bytes(buf[i * shard_len:(i + 1) * shard_len])
            for i in range(k)], len(data)

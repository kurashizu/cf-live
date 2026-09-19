# Cross-border UDP relay

Serves China viewers from a host inside China, fed over UDP with forward
error correction. Plain HLS over HTTPS does not work on that path: a viewer
probe fetched 30 fragments and all 30 returned 404, because each poll cycle
took a median of 12 s against a 3 s window.

## Why UDP

The same path measured three ways:

| | RTT | jitter | loss |
|---|---|---|---|
| ICMP | 281 ms | 0.33 ms | (rate-limited) |
| **UDP** | **280 ms** | **1.2 ms** | 16-23% |
| TCP connect | 288 ms | **730 ms** | 0% |

The link itself is clean. TCP handshakes are what get held up, and HLS is
HTTP over TCP, so no amount of buffering fixes it without adding tens of
seconds of latency. UDP keeps the latency but drops one datagram in five.

## Why forward correction, not retransmission

A retransmit costs a 281 ms round trip, by which time the segment has left
the live window. So parity is sent unconditionally and there is no feedback
channel at all — no ACK, no NACK.

That trade only works because of two measured properties:

- **Loss is rate-independent** — 22.7% at 5 pps, 21.0% at 400 pps. It is not
  congestion, so adding redundancy does not make it worse. Had it been
  congestion, sending more would have been an avalanche.
- **Loss arrives as isolated singles** — 167 runs, lengths 1:144 2:18 3:2
  4:3, longest 4. Bursts are what defeat block codes; these barely qualify.

Shards are interleaved across blocks so a run lands in different blocks
rather than exhausting one block's parity: measured 3.99% vs 9.94% segment
loss at 23%, for free.

## Parameters

`RS(40,80)` — 40 data shards, 40 parity, 100% overhead.

Larger blocks beat more redundancy here. At the same 100% overhead,
RS(10,20) fails when 11 of 20 shards are lost (4.3 sigma from the mean at
23% loss) while RS(40,80) needs 41 of 80 (5.9 sigma). A bigger sample
deviates proportionally less, and fewer blocks per segment means fewer
chances for any one to fail. Simulated segment loss at 25%:

| config | overhead | P(one lost segment in 5 min) |
|---|---|---|
| RS(10,20) | 100% | 100% |
| RS(20,40) | 100% | 78% |
| **RS(40,80)** | **100%** | **1.3%** |

Note the cliff: at 30% loss RS(40,80) degrades to 61%. Its safe operating
range is roughly up to 27%, against a measured 16-23%.

Shards are 1200 B so the datagram stays under the 1280 B IPv6 minimum MTU.
Fragmenting would be worse than it sounds — a fragmented datagram is lost
entirely if any fragment is, which would compound the loss rate.

## Direction

The security group in front of the China host drops unsolicited inbound UDP
(all 16 probed ports) but is stateful, so return traffic flows on a
connection it opened. **The receiver initiates and keeps the pinhole alive;
the sender waits to learn the peer.** Verified holding 62 s at 0.96 Mbps
with 5 s keepalives.

Inbound TCP is worse: all 25 probed ports blocked, only 80 and 443 open and
both already used by an unrelated site. So nginx serves the relay on port 80
as `default_server`, which answers bare-IP requests while that site keeps
its own `server_name` and its redirect.

## Running it

Sender, on the origin VPS:

```sh
python3 send.py --root /dev/shm/cf-live --stream main \
  --port 39997 --k 40 --n 80 --pace 0.25
```

Receiver, on the China host (systemd unit in `krsz-relay.service`):

```sh
python3 recv.py --sender <vps-ip> --sender-port 39997 \
  --local-port 45010 --http-port 9990 --window 3
```

Then `nginx-krsz.conf` into `/etc/nginx/conf.d/`, and `nginx -t` before
reloading.

## Tests

```sh
python3 test_rs.py        # codec: field axioms, exhaustive RS(4,8), randomised
./test-live.sh            # end to end against the deployed pair
python3 continuity.py http://127.0.0.1:9990 25   # a viewer's timeline
```

`continuity.py` is the one that matters for playback: it checks that
fragment indices arrive in order with no skips, that everything advertised
downloads, and that the playlist never freezes.

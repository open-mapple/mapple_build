#!/usr/bin/env python3
"""A raw HFS+ root image as an Android sparse image of its allocated blocks, for fastboot flash.

  rootranges.py IMG OUT.img RANGES

OUT.img (- for stdout) holds the allocated blocks as raw chunks and skips the rest; its size is the volume's.
RANGES gets the block size, the volume's size in blocks, the SHA-256 of the allocated blocks in order and their
ranges, for mapple-install verify-root. The first and last blocks (the volume header and the alternate one) are
always in: fastboot writes the chunks in order, so the alternate header is the last thing a complete flash writes.
"""
import hashlib
import struct
import sys

CHUNK = 4096    # blocks per raw chunk


def ranges(f):
    f.seek(1024)
    h = f.read(512)
    assert h[:2] in (b'H+', b'HX'), 'not an HFS+ volume'
    bs, total = struct.unpack('>II', h[40:48])
    fork = h[112:192]       # the allocation file's fork: its extents hold the bitmap, one bit per block, MSB first
    bitmap = bytearray()
    for i in range(8):
        start, count = struct.unpack('>II', fork[16 + 8 * i:24 + 8 * i])
        f.seek(start * bs)
        bitmap += f.read(count * bs)
    bitmap[0] |= 0x80
    bitmap[(total - 1) >> 3] |= 0x80 >> ((total - 1) & 7)
    out, run = [], None
    for blk in range(total):
        used = bitmap[blk >> 3] >> (7 - (blk & 7)) & 1
        if used and run is None:
            run = blk
        elif not used and run is not None:
            out.append((run, blk))
            run = None
    if run is not None:
        out.append((run, total))
    return bs, total, out


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    img, out_path, ranges_path = sys.argv[1:]
    f = open(img, 'rb')
    bs, total, rs = ranges(f)
    chunks = []         # (raw, start, blocks)
    pos = 0
    for a, b in rs:
        if a > pos:
            chunks.append((False, pos, a - pos))
        chunks += [(True, s, min(CHUNK, b - s)) for s in range(a, b, CHUNK)]
        pos = b
    if pos < total:
        chunks.append((False, pos, total - pos))
    out = sys.stdout.buffer if out_path == '-' else open(out_path, 'wb')
    out.write(struct.pack('<IHHHHIIII', 0xed26ff3a, 1, 0, 28, 12, bs, total, len(chunks), 0))
    sha = hashlib.sha256()
    for raw, start, n in chunks:
        if not raw:
            out.write(struct.pack('<HHII', 0xcac3, 0, n, 12))
            continue
        out.write(struct.pack('<HHII', 0xcac1, 0, n, 12 + n * bs))
        f.seek(start * bs)
        data = f.read(n * bs)
        sha.update(data)
        out.write(data)
    out.flush()
    with open(ranges_path, 'w') as r:
        r.write(f'mapple-root 1\nblock_size {bs}\nblocks {total}\nsha256 {sha.hexdigest()}\n')
        r.writelines(f'range {a} {b}\n' for a, b in rs)
    print(f'{img}: {sum(b - a for a, b in rs)} of {total} blocks in {len(rs)} ranges', file=sys.stderr)


if __name__ == '__main__':
    main()

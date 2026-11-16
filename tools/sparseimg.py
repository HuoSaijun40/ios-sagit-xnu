#!/usr/bin/env python3
"""Android sparse image helpers for the iOS root workflow.

Subcommands:
  make     raw.img out.simg    convert a raw image to Android sparse
  unsparse out.simg raw.img    convert a sparse image back to raw
  check    raw.img             verify the HFS+ volume header and sizes

The sparse format is documented in AOSP system/core/libsparse/sparse_format.h.
fastboot accepts sparse images for `flash`, which is what keeps the uploaded
artifact small: free space in the HFS+ volume becomes DONT_CARE chunks.
"""
import os
import struct
import sys

MAGIC = 0xED26FF3A
CHUNK_RAW = 0xCAC1
CHUNK_FILL = 0xCAC2
CHUNK_DONT_CARE = 0xCAC3
BLOCK = 4096
FILE_HDR_SZ = 28
CHUNK_HDR_SZ = 12


def _classify(path):
    """Group the image into maximal raw / dont-care runs."""
    size = os.path.getsize(path)
    if size % BLOCK:
        sys.exit("error: %s is not a multiple of %d bytes" % (path, BLOCK))
    total = size // BLOCK
    zero = bytes(BLOCK)
    chunks = []
    with open(path, "rb") as f:
        for blk in range(total):
            data = f.read(BLOCK)
            if len(data) != BLOCK:
                sys.exit("error: short read at block %d" % blk)
            kind = CHUNK_DONT_CARE if data == zero else CHUNK_RAW
            if chunks and chunks[-1][0] == kind:
                chunks[-1][2] += 1
            else:
                chunks.append([kind, blk, 1])
    return total, chunks


def cmd_make(src, dst):
    total, chunks = _classify(src)
    data_bytes = sum(n * BLOCK for k, s, n in chunks if k == CHUNK_RAW)
    blob = FILE_HDR_SZ + len(chunks) * CHUNK_HDR_SZ + data_bytes

    with open(dst, "wb") as out, open(src, "rb") as f:
        out.write(struct.pack("<IHHHHIIII", MAGIC, 1, 0, FILE_HDR_SZ, CHUNK_HDR_SZ,
                              BLOCK, total, len(chunks), blob))
        for kind, start, count in chunks:
            if kind == CHUNK_RAW:
                out.write(struct.pack("<HHII", kind, 0, CHUNK_HDR_SZ + count * BLOCK, count))
                f.seek(start * BLOCK)
                left = count * BLOCK
                while left:
                    buf = f.read(min(left, 8 << 20))
                    if not buf:
                        sys.exit("error: unexpected EOF")
                    out.write(buf)
                    left -= len(buf)
            else:
                out.write(struct.pack("<HHII", kind, 0, CHUNK_HDR_SZ, count))

    raw_sz = os.path.getsize(src)
    simg_sz = os.path.getsize(dst)
    print("raw    : %d bytes (%.2f GiB)" % (raw_sz, raw_sz / 2**30))
    print("sparse : %d bytes (%.2f GiB) = %.1f%% of raw"
          % (simg_sz, simg_sz / 2**30, 100.0 * simg_sz / raw_sz))
    print("chunks : %d" % len(chunks))


def cmd_unsparse(src, dst):
    with open(src, "rb") as f:
        magic, major, minor, fhdr, chdr, blk, total, nchunks, _ = \
            struct.unpack("<IHHHHIIII", f.read(FILE_HDR_SZ))
        if magic != MAGIC:
            sys.exit("error: bad sparse magic %#x" % magic)
        if blk != BLOCK:
            sys.exit("error: block size %d != %d" % (blk, BLOCK))
        with open(dst, "wb") as out:
            for i in range(nchunks):
                ctype, _, csize, n = struct.unpack("<HHII", f.read(CHUNK_HDR_SZ))
                nbytes = n * BLOCK
                if ctype == CHUNK_RAW:
                    left = nbytes
                    while left:
                        buf = f.read(min(left, 8 << 20))
                        if not buf:
                            sys.exit("error: truncated raw chunk")
                        out.write(buf)
                        left -= len(buf)
                elif ctype == CHUNK_FILL:
                    pat = f.read(4)
                    out.write(pat * (nbytes // 4))
                elif ctype == CHUNK_DONT_CARE:
                    out.seek(nbytes, os.SEEK_CUR)
                else:
                    sys.exit("error: bad chunk type %#x" % ctype)
    print("unsparse: %s -> %s" % (src, dst))


def cmd_check(path):
    with open(path, "rb") as f:
        head = f.read(2048)
    vh = head[1024:1536]      # HFS+ volume header sits at offset 1024
    sig = vh[0:2]
    if sig == b"H+":
        kind = "HFS+ (case-insensitive)"
    elif sig == b"HX":
        kind = "HFSX (case-sensitive)"
    else:
        sys.exit("error: no HFS+ volume header at offset 1024 (sig=%r)" % sig)
    ver, attrs = struct.unpack_from(">HH", vh, 2)
    blocksize, = struct.unpack_from(">I", vh, 40)
    totalblocks, = struct.unpack_from(">I", vh, 44)
    freeblocks, = struct.unpack_from(">I", vh, 48)
    print("filesystem   : %s" % kind)
    print("version      : %d" % ver)
    print("attributes   : %#06x%s" % (attrs, "  (kHFSVolumeUnmountedBit set)" if attrs & 0x100 else ""))
    print("block size   : %d" % blocksize)
    print("total blocks : %d" % totalblocks)
    print("free blocks  : %d" % freeblocks)
    print("volume bytes : %.2f GiB" % (blocksize * totalblocks / 2**30))
    print("used bytes   : %.2f GiB" % (blocksize * (totalblocks - freeblocks) / 2**30))
    if (blocksize * (totalblocks - freeblocks)) < (8 << 30):
        print("warning: volume holds less than 8 GiB of data; copy may be incomplete")


def main(argv):
    if len(argv) == 4 and argv[1] == "make":
        cmd_make(argv[2], argv[3])
    elif len(argv) == 4 and argv[1] == "unsparse":
        cmd_unsparse(argv[2], argv[3])
    elif len(argv) == 3 and argv[1] == "check":
        cmd_check(argv[2])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)

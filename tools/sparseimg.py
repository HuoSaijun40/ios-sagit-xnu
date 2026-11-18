#!/usr/bin/env python3
"""Android sparse image helpers for the iOS root workflow.

Subcommands:
  make     raw.img out.simg    convert a raw image to Android sparse
  unsparse out.simg raw.img    convert a sparse image back to raw
  check    raw.img             verify the HFS+ volume header and sizes
  info     out.simg            print (or --field print one of) the sparse header

The sparse format is documented in AOSP system/core/libsparse/sparse_format.h.
fastboot accepts sparse images for `flash`, which is what keeps the uploaded
artifact small: free space in the HFS+ volume becomes DONT_CARE chunks.

Two properties matter more than speed here:

  * The header fields are 32-bit. A 12 GiB image with hundreds of thousands of
    scattered chunks overflows `blob_size` (header + chunk headers + data) and
    `struct.pack` raises ``'I' format requires 0 <= number <= 4294967295``.
    That is exactly what killed run 12 of the workflow, so every field written
    is now range-checked first and the failure explains itself. Callers can
    also cap the output with --target-size, which drops the free tail of the
    volume: the raw image is 12 GiB but only ~8.4 GiB is in use, and the free
    tail is pure DONT_CARE.
  * Classification must not be one Python call per 4 KiB block. 4 MiB is
    scanned per ``bytes.translate`` call, so a full-size volume takes a
    couple of seconds instead of minutes.
"""
import argparse
import os
import struct
import sys

MAGIC = 0xED26FF3A
CHUNK_RAW = 0xCAC1
CHUNK_FILL = 0xCAC2
CHUNK_DONT_CARE = 0xCAC3
BLOCK = 4096
# Block size of the produced image may be raised with --block-size; fastboot
# accepts any power-of-two block size the sparse header can carry (libsparse
# supports 4096 and 8192 upstream).
FILE_HDR_SZ = 28
CHUNK_HDR_SZ = 12

U32_MAX = 0xFFFFFFFF

# How much to read per classify() iteration. Must be a multiple of BLOCK.
SCAN = 4 << 20


def _range(name, value):
    """Every sparse header field is a 32-bit unsigned int."""
    if not (0 <= value <= U32_MAX):
        sys.exit(
            "error: %s = %d does not fit a 32-bit field (max %d = %.2f GiB).\n"
            "       Android sparse caps the WHOLE image at 4 GiB, data included,\n"
            "       so this is not about overhead or fragmentation: the volume\n"
            "       holds %.2f GiB. Options: (a) flash a plain raw image instead\n"
            "       of sparse, or (b) shrink volume_size so the used data fits\n"
            "       in 4 GiB."
            % (name, value, U32_MAX, U32_MAX / 2**30, value / 2**30))
    return value


# bytes.translate table: every byte maps to 1 (non-zero) or 0 (zero).
_NZ_TABLE = bytes(0 if i == 0 else 1 for i in range(256))


def _classify(path, target_bytes, block=BLOCK):
    """Group the image into maximal raw / dont-care runs.

    Returns (total_blocks, chunks) where each chunk is [kind, first_block, count]
    and counts are in units of ``block`` bytes. Only the first ``target_bytes``
    are considered when given (must be a multiple of ``block``).
    """
    size = os.path.getsize(path)
    if size % block:
        sys.exit("error: %s is not a multiple of the %d-byte block size"
                 % (path, block))
    total = size // block
    if target_bytes is not None:
        if target_bytes <= 0 or target_bytes > size or target_bytes % block:
            sys.exit("error: target %d bytes is not a usable multiple of %d "
                     "within %d bytes" % (target_bytes, block, size))
        total = target_bytes // block

    zero = bytes(block)
    chunks = []
    done = 0
    with open(path, "rb") as f:
        while done < total:
            want = min(SCAN, (total - done) * block)
            data = f.read(want)
            if len(data) != want:
                sys.exit("error: short read at block %d" % done)
            # A sparse chunk covers whole blocks, so a block is either RAW or
            # DONT_CARE: RAW if ANY of its bytes is non-zero.
            flags = bytes(_block_flag(data[k:k + block], zero)
                          for k in range(0, len(data), block))
            nblk = len(flags)
            i = 0
            while i < nblk:
                kind_byte = flags[i]
                j = i + 1
                while j < nblk and flags[j] == kind_byte:
                    j += 1
                kind = CHUNK_RAW if kind_byte else CHUNK_DONT_CARE
                _append(chunks, kind, done + i, j - i)
                i = j
            done += nblk
    return total, chunks


def _append(chunks, kind, start, count):
    if count <= 0:
        return
    if chunks and chunks[-1][0] == kind and chunks[-1][1] + chunks[-1][2] == start:
        chunks[-1][2] += count
    else:
        chunks.append([kind, start, count])


# bytes.translate table: every byte maps to 1 (non-zero) or 0 (zero).
_NZ_TABLE = bytes(0 if i == 0 else 1 for i in range(256))

def _block_flag(chunk, zero):
    """1 if any byte of this chunk is non-zero, else 0.

    A sparse chunk covers whole blocks, so the decision is per block, not per
    byte: an earlier revision tested bytes individually and split block 0 --
    the HFS+ signature followed by zeros -- into both RAW and DONT_CARE.
    """
    return 0 if chunk == zero else 1


def _used_blocks(path):
    """HFS+ volume header: totalBlocks at 44, freeBlocks at 48 (big endian).

    Blocks are counted from the start of the volume. HFS+ allocates from the
    start upward, so the free space is normally one contiguous tail; keeping
    the first (total - free) blocks therefore keeps every allocated block and
    drops only free space. Anything the allocator placed above that line would
    be lost, so `make` still classifies the whole kept region (nothing is
    skipped blindly) and the caller can pass --target-size to override.
    """
    with open(path, "rb") as f:
        f.seek(1024)
        vh = f.read(512)
    if vh[0:2] not in (b"H+", b"HX"):
        return None, None
    total, = struct.unpack_from(">I", vh, 44)
    free, = struct.unpack_from(">I", vh, 48)
    return total, free


def cmd_make(src, dst, target_size=None, block=BLOCK):
    target_bytes = target_size
    if target_bytes is None:
        vtotal, vfree = _used_blocks(src)
        if vtotal is not None and vfree is not None:
            target_bytes = (vtotal - vfree) * BLOCK
            print("volume: %d blocks total, %d free -> keeping %d blocks (%.2f GiB)"
                  % (vtotal, vfree, vtotal - vfree, target_bytes / 2**30))

    total, chunks = _classify(src, target_bytes, block)
    data_bytes = sum(n * block for k, s, n in chunks if k == CHUNK_RAW)
    blob = _range("blob_size",
                  FILE_HDR_SZ + len(chunks) * CHUNK_HDR_SZ + data_bytes)
    _range("total_chunks", len(chunks))
    _range("total_blocks", total)

    with open(dst, "wb") as out, open(src, "rb") as f:
        out.write(struct.pack("<IHHHHIIII", MAGIC, 1, 0, FILE_HDR_SZ, CHUNK_HDR_SZ,
                             block, total, len(chunks), blob))
        for kind, start, count in chunks:
            if kind == CHUNK_RAW:
                out.write(struct.pack("<HHII", kind, 0, CHUNK_HDR_SZ + count * block, count))
                f.seek(start * block)
                left = count * block
                while left:
                    buf = f.read(min(left, 8 << 20))
                    if not buf:
                        sys.exit("error: unexpected EOF")
                    out.write(buf)
                    left -= len(buf)
            else:
                out.write(struct.pack("<HHII", kind, 0, CHUNK_HDR_SZ, count))

    raw_sz = total * block
    simg_sz = os.path.getsize(dst)
    print("raw    : %d bytes (%.2f GiB)" % (raw_sz, raw_sz / 2**30))
    print("sparse : %d bytes (%.2f GiB) = %.1f%% of raw"
          % (simg_sz, simg_sz / 2**30, 100.0 * simg_sz / raw_sz))
    print("chunks : %d" % len(chunks))


def _sparse_header(path):
    """Read and validate the 28-byte sparse file header."""
    with open(path, "rb") as f:
        magic, major, minor, fhdr, chdr, blk, total, nchunks, blob = \
            struct.unpack("<IHHHHIIII", f.read(FILE_HDR_SZ))
    if magic != MAGIC:
        sys.exit("error: %s is not an Android sparse image (magic %#x)"
                 % (path, magic))
    if blk not in (4096, 8192, 16384, 32768, 65536):
        sys.exit("error: unsupported block size %d" % blk)
    return {"magic": magic, "major": major, "minor": minor,
            "file_hdr_sz": fhdr, "chunk_hdr_sz": chdr, "block_size": blk,
            "total_blocks": total, "total_chunks": nchunks,
            "blob_size": blob}


def cmd_info(path, field=None):
    h = _sparse_header(path)
    if field:
        if field not in h:
            sys.exit("error: unknown field %r" % field)
        print(h[field])
        return
    actual = os.path.getsize(path)
    for k in ("magic", "major", "minor", "block_size", "total_blocks",
              "total_chunks", "blob_size"):
        print("%-14s: %d" % (k, h[k]))
    print("%-14s: %d%s" % ("file size", actual,
                           "" if actual == h["blob_size"] else "   MISMATCH"))
    print("%-14s: %.2f GiB" % ("image size",
                               h["total_blocks"] * BLOCK / 2**30))


def cmd_unsparse(src, dst):
    with open(src, "rb") as f:
        magic, major, minor, fhdr, chdr, blk, total, nchunks, _ = \
            struct.unpack("<IHHHHIIII", f.read(FILE_HDR_SZ))
        if magic != MAGIC:
            sys.exit("error: bad sparse magic %#x" % magic)
        if blk not in (4096, 8192, 16384, 32768, 65536):
            sys.exit("error: unsupported block size %d" % blk)
        with open(dst, "wb") as out:
            for i in range(nchunks):
                ctype, _, csize, n = struct.unpack("<HHII", f.read(CHUNK_HDR_SZ))
                nbytes = n * blk
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
            # Trailing DONT_CARE chunks leave holes; make the file the full
            # advertised size so a straight byte compare is meaningful.
            out.truncate(total * blk)
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
    # What `make` will actually keep when the free tail is dropped.
    used = totalblocks - freeblocks
    print("sparse target: %d of %d blocks (%.2f GiB) after dropping the free tail"
          % (used, totalblocks, blocksize * used / 2**30))


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    sub = ap.add_subparsers(dest="cmd")

    m = sub.add_parser("make")
    m.add_argument("src")
    m.add_argument("dst")
    m.add_argument("--target-size", type=int, default=None,
                   help="number of 4096-byte blocks to keep (default: the HFS+ "
                        "volume's used region, i.e. drop the free tail)")
    m.add_argument("--target-size-gib", type=float, default=None,
                   help="same, expressed in GiB")
    m.add_argument("--block-size", type=int, default=BLOCK,
                   help="sparse chunk block size (default 4096). The whole " 
                        "output must fit a 32-bit blob_size, so a large image " 
                        "needs 8192 or more here; fastboot and libsparse " 
                        "accept powers of two.")

    u = sub.add_parser("unsparse")
    u.add_argument("src")
    u.add_argument("dst")

    c = sub.add_parser("check")
    c.add_argument("src")

    i = sub.add_parser("info")
    i.add_argument("src")
    i.add_argument("--field", default=None,
                   help="print just this header field (e.g. total_blocks)")

    args = ap.parse_args(argv[1:])
    if args.cmd == "make":
        target = args.target_size
        if args.target_size_gib:
            target = int(args.target_size_gib * 2**30)
        cmd_make(args.src, args.dst, target, args.block_size)
    elif args.cmd == "unsparse":
        cmd_unsparse(args.src, args.dst)
    elif args.cmd == "check":
        cmd_check(args.src)
    elif args.cmd == "info":
        cmd_info(args.src, args.field)
    else:
        ap.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv)

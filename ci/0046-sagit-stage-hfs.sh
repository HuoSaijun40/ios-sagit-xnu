#!/bin/bash
# Stage Apple's open source HFS for the SAGIT built-in filesystem (bsd/sagit_hfs).
# dxb runs patches/*.sh from the XNU source root before applying *.patch.
set -euo pipefail
# Vendored sources live next to xnu/ in the repo (no network needed).
VENDOR="${VENDOR:-${GITHUB_WORKSPACE}/vendor}"

[[ "${MACHINE_CONFIG:-}" == SAGIT ]] || exit 0
[[ "${MACOS_VERSION:-}" == 13.5 ]] || {
    echo 'SAGIT HFS integration requires xnu-8796.141.3 (macOS 13.5)' >&2
    exit 1
}
[[ -f bsd/kern/bsd_init.c ]] || exit 1

# hfs-627.100.6 is paired with xnu-8796.141.3 in Apple's macos-135
# release.json. Fetch the immutable commit, never a moving branch/tag.
hfs_sha=31d5ec238dfab485e78a9a30b8b92d13e8914f03
# Sources are committed as vendor/hfs.tar.gz (no network needed).
if [ ! -f "$VENDOR/hfs.tar.gz" ]; then echo "missing vendor/hfs.tar.gz" >&2; exit 1; fi
hfs_tmp=$(mktemp -d)
tar -xzf "$VENDOR/hfs.tar.gz" -C "$hfs_tmp"
trap 'rm -rf "$hfs_tmp"' EXIT
echo "using vendored hfs-627.100.6"



python3 - "$hfs_tmp/hfs" <<'PY'
from pathlib import Path
import shutil
import sys

source = Path(sys.argv[1])
target = Path('bsd/sagit_hfs')
if target.exists():
    print(f'already staged: {target}'); raise SystemExit(0)
# Preserve upstream copyright/license headers; no downloaded sources in git.
for directory in ('core', 'hfs_encodings'):
    shutil.copytree(source / directory, target / directory)

# SAGIT has no AppleKeyStore (content protection is not built, see
# sagit_hfs_compat.h); its header is only needed for the key-wrapping API.
path = target / 'core' / 'hfs_iokit.h'
text = path.read_text()
include = '#include <AppleKeyStore/AppleKeyStoreFSServices.h>\n'
first = 'int hfs_unwrap_key('
end = '__END_DECLS'
if text.count(include) != 1 or text.count(first) != 1 or text.count(end) != 1:
    raise SystemExit(f'HFS source changed: {path}')
text = text.replace(include, '#if CONFIG_PROTECT\n' + include + '#endif\n')
i, j = text.index(first), text.index(end)
text = text[:i] + '#if CONFIG_PROTECT\n' + text[i:j].rstrip('\n') + '\n#endif\n\n' + text[j:]
path.write_text(text)
PY

# Kernel builds use -nostdinc; HFS's kext-config.h needs the SDK's target macros.
hfs_sdk=$(xcrun --sdk macosx --show-sdk-path)
cp "$hfs_sdk/usr/include/TargetConditionals.h" bsd/sagit_hfs/
echo "Staged hfs-627.100.6 ($hfs_sha) for SAGIT"

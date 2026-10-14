#!/usr/bin/env python3
"""A stand-in for the `age` CLI (only the flags glassboard uses), for tests on hosts without age.
Encrypt: age -r R [-r R…] -o OUT IN    Decrypt: age -d -i IDENTITY -o OUT IN
Not cryptography: base64 behind the real age header, recipients listed; decrypt checks the identity."""
import base64
import sys

args = sys.argv[1:]
out = args[args.index("-o") + 1]
src = args[-1]
if "-d" in args:
    ident = open(args[args.index("-i") + 1]).read().strip()
    lines = open(src, "rb").read().split(b"\n")
    assert lines[0] == b"age-encryption.org/v1", "not an age file"
    if ident.encode() not in lines[1].split(b","):
        sys.stderr.write("no identity matched any of the recipients\n")
        sys.exit(1)
    open(out, "wb").write(base64.b64decode(lines[2]))
else:
    recips = [args[i + 1] for i, a in enumerate(args) if a == "-r"]
    data = open(src, "rb").read()
    open(out, "wb").write(b"age-encryption.org/v1\n" + ",".join(recips).encode() + b"\n" + base64.b64encode(data))

"""Seal an SSH backup stream locally with AES-256-GCM; never print key material.

Use operator-owned private directories on a filesystem supporting hard links.
Decrypt stages owner-only plaintext before authentication, but only publishes
its named destination after tag verification. SIGKILL/power loss can leave a
private .sealed-backup-* orphan; never treat that orphan as authenticated data.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b'CWB-AESGCM-1\n'
LIMIT = 4 * 1024**3


def read_key(path, create=False):
    if create and not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(os.urandom(32)); output.flush(); os.fsync(output.fileno())
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size != 32):
            raise ValueError('Key must be an owner-only regular 32-byte file')
        key = os.read(fd, 33)
        if len(key) != 32:
            raise ValueError('Key read must contain exactly 32 bytes')
        return key
    finally: os.close(fd)


def transform(source, output_path, key, decrypt=False):
    if len(key) != 32:
        raise ValueError('AES-256-GCM requires a 32-byte key')
    if output_path.exists() or output_path.is_symlink():
        raise ValueError('Destination must not exist')
    fd, temporary = tempfile.mkstemp(prefix='.sealed-backup-', dir=output_path.parent)
    consumed = 0
    try:
        with os.fdopen(fd, 'wb') as output:
            if decrypt:
                size = source.seek(0, 2)
                source.seek(0)
                if source.read(len(MAGIC)) != MAGIC or size < len(MAGIC)+12+16:
                    raise ValueError('Unsupported backup envelope')
                nonce = source.read(12)
                source.seek(-16,2); tag = source.read(16)
                source.seek(len(MAGIC)+12)
                remaining = size-len(MAGIC)-12-16
                if remaining > LIMIT: raise ValueError('Backup exceeds envelope limit')
                crypt = Cipher(algorithms.AES(key), modes.GCM(nonce,tag)).decryptor()
            else:
                nonce = os.urandom(12)
                output.write(MAGIC+nonce)
                crypt = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
                remaining = LIMIT+1
            crypt.authenticate_additional_data(MAGIC)
            while remaining:
                chunk = source.read(min(1024**2,remaining))
                if not chunk: break
                remaining -= len(chunk); consumed += len(chunk)
                if consumed > LIMIT: raise ValueError('Backup exceeds envelope limit')
                output.write(crypt.update(chunk))
            if decrypt and remaining: raise ValueError('Truncated backup')
            output.write(crypt.finalize())
            if not decrypt: output.write(crypt.tag)
            output.flush(); os.fsync(output.fileno())
        os.link(temporary,output_path)
        parent=os.open(output_path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        os.unlink(temporary)
    digest=hashlib.sha256()
    with output_path.open('rb') as value:
        while chunk:=value.read(1024**2):digest.update(chunk)
    return {'operation':'open' if decrypt else 'seal','input_bytes':consumed,
            'output_bytes':output_path.stat().st_size,'output_sha256':digest.hexdigest(),
            'authenticated_encryption':'AES-256-GCM','key_material_returned':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['seal','open'])
    parser.add_argument('--key-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--input',type=Path)
    parser.add_argument('--create-key',action='store_true')
    args=parser.parse_args()
    if args.operation=='open' and (not args.input or args.create_key):
        parser.error('open requires --input and an existing key')
    key=read_key(args.key_file,args.create_key)
    source=args.input.open('rb') if args.input else sys.stdin.buffer
    try:receipt=transform(source,args.output,key,args.operation=='open')
    finally:
        if args.input:source.close()
    print(json.dumps(receipt,indent=2))


if __name__=='__main__':main()

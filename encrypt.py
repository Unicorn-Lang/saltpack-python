#! /usr/bin/env python3

import binascii
import base64
from hashlib import sha512
import io
import json
import os
import sys

import umsgpack
import nacl.bindings
from nacl.exceptions import CryptoError
import docopt

__doc__ = '''\
Usage:
    encrypt.py encrypt [<private>] [<recipients>...] [options]
    encrypt.py decrypt [<private>] [options]

If no private key is given, the default is 32 zero bytes. If no recipient is
given, the default is the sender's own public key.

Options:
    -c --chunk=<size>   size of payload chunks, default 1 MB
    -m --message=<msg>  message text, instead of reading stdin
    --debug             debug mode
'''

FORMAT_VERSION = 1

# Hardcode the keys for everyone involved.
# ----------------------------------------

jack_private = b'\xaa' * 32


# Utility functions.
# ------------------

def chunks_with_empty(message, chunk_size):
    'The last chunk is empty, which signifies the end of the message.'
    chunk_start = 0
    chunks = []
    while chunk_start < len(message):
        chunks.append(message[chunk_start:chunk_start+chunk_size])
        chunk_start += chunk_size
    # empty chunk
    chunks.append(b'')
    return chunks


def json_repr(obj):
    # We need to repr everything that JSON doesn't directly support,
    # particularly bytes.
    def _recurse_repr(obj):
        if isinstance(obj, (list, tuple)):
            return [_recurse_repr(x) for x in obj]
        elif isinstance(obj, dict):
            return {_recurse_repr(key): _recurse_repr(val)
                    for key, val in obj.items()}
        elif isinstance(obj, bytes):
            try:
                obj.decode('utf8')
                return repr(obj)
            except UnicodeDecodeError:
                return repr(base64.b64encode(obj))
        else:
            return obj
    return json.dumps(_recurse_repr(obj), indent='  ')


def counter(i):
    'Turn the number into a 64-bit big-endian unsigned representation.'
    return i.to_bytes(8, 'big')


# All the important bits!
# -----------------------

def encrypt(sender_private, recipient_public_keys, message, chunk_size):
    sender_public = nacl.bindings.crypto_scalarmult_base(sender_private)
    ephemeral_private = os.urandom(32)
    ephemeral_public = nacl.bindings.crypto_scalarmult_base(ephemeral_private)
    nonce_prefix_preimage = (
        b"SaltPack\0" +
        b"encryption nonce prefix\0" +
        ephemeral_public)
    nonce_prefix = sha512(nonce_prefix_preimage).digest()[:16]
    encryption_key = os.urandom(32)

    keys = [sender_public, encryption_key]
    keys_bytes = umsgpack.packb(keys)
    header_nonce = nonce_prefix + counter(0)

    recipient_pairs = []
    recipient_beforenms = {}
    for recipient_public in recipient_public_keys:
        # The recipient box holds the sender's long-term public key and the
        # symmetric message encryption key. It's encrypted for each recipient
        # with the ephemeral private key.
        recipient_box = nacl.bindings.crypto_box(
            message=keys_bytes,
            nonce=header_nonce,
            pk=recipient_public,
            sk=ephemeral_private)
        # None is for the recipient public key, which is optional.
        pair = [None, recipient_box]
        recipient_pairs.append(pair)

        # Precompute the shared secret to speed up payload packet encryption.
        beforenm = nacl.bindings.crypto_box_beforenm(
            pk=recipient_public,
            sk=sender_private)
        recipient_beforenms[recipient_public] = beforenm

    header = [
        "SaltBox",  # format name
        [1, 0],     # major and minor version
        0,          # mode (encryption, as opposed to signing/detached)
        ephemeral_public,
        recipient_pairs,
    ]
    output = io.BytesIO()
    output.write(umsgpack.packb(header))

    # Write the chunks.
    for packetnum, chunk in enumerate(chunks_with_empty(message, chunk_size)):
        payload_nonce = nonce_prefix + counter(packetnum + 2)
        payload_secretbox = nacl.bindings.crypto_secretbox(
            message=chunk,
            nonce=payload_nonce,
            key=encryption_key)
        payload_tag = payload_secretbox[:16]  # the Poly1305 authenticator
        stripped_payload_secretbox = payload_secretbox[16:]
        tag_boxes = []
        for recipient_public in recipient_public_keys:
            # Encrypt the payload_tag for each recipient. This isn't because we
            # want to keep the tag secret, but because:
            #   1) We want to authenticate the tag, to prove the sender
            #      actually wrote it.
            #   2) We want to force implementations to verify that.
            beforenm = recipient_beforenms[recipient_public]
            tag_box = nacl.bindings.crypto_box_afternm(
                message=payload_tag,
                nonce=payload_nonce,
                k=beforenm)
            tag_boxes.append(tag_box)
        packet = [
            tag_boxes,
            stripped_payload_secretbox,
        ]
        output.write(umsgpack.packb(packet))

    return output.getvalue()


def decrypt(input, recipient_private, *, debug=False):
    stream = io.BytesIO(input)
    # Parse the header.
    header = umsgpack.unpack(stream)
    if debug:
        print('Header: ', end='', file=sys.stderr)
        print(json_repr(header), file=sys.stderr)
    [
        format_name,
        [major_version, minor_version],
        mode,
        ephemeral_public,
        recipient_pairs,
    ] = header
    nonce_prefix_preimage = (
        b"SaltPack\0" +
        b"encryption nonce prefix\0" +
        ephemeral_public)
    nonce_prefix = sha512(nonce_prefix_preimage).digest()[:16]
    ephemeral_beforenm = nacl.bindings.crypto_box_beforenm(
        pk=ephemeral_public,
        sk=recipient_private)

    # Try decrypting each sender box, until we find the one that works.
    for recipient_index, pair in enumerate(recipient_pairs):
        [_, recipient_box] = pair
        try:
            keys_bytes = nacl.bindings.crypto_box_open_afternm(
                ciphertext=recipient_box,
                nonce=nonce_prefix + counter(0),
                k=ephemeral_beforenm)
            break
        except CryptoError:
            continue
    else:
        raise RuntimeError('Failed to find matching recipient.')

    # Unpack the sender key and the message encryption key.
    keys = umsgpack.unpackb(keys_bytes)
    sender_public, encryption_key = keys

    # Precompute the shared secret to speed up payload decryption.
    sender_beforenm = nacl.bindings.crypto_box_beforenm(
        pk=sender_public,
        sk=recipient_private)

    # Decrypt each of the packets.
    output = io.BytesIO()
    packetnum = 2
    while True:
        payload_nonce = nonce_prefix + counter(packetnum)
        packet = umsgpack.unpack(stream)
        if debug:
            print('Packet: ', end='', file=sys.stderr)
            print(json_repr(packet), file=sys.stderr)
        [tag_boxes, stripped_payload_secretbox] = packet
        tag_box = tag_boxes[recipient_index]

        # Open the tag box.
        payload_tag = nacl.bindings.crypto_box_open_afternm(
            ciphertext=tag_box,
            nonce=payload_nonce,
            k=sender_beforenm)

        # Prepend the tag and open the payload secretbox.
        payload_secretbox = payload_tag + stripped_payload_secretbox
        chunk = nacl.bindings.crypto_secretbox_open(
            ciphertext=payload_secretbox,
            nonce=payload_nonce,
            key=encryption_key)
        output.write(chunk)
        if debug:
            print('Chunk:', chunk, file=sys.stderr)

        # The empty chunk signifies the end of the message.
        if chunk == b'':
            break

        packetnum += 1

    return output.getvalue()


def get_private(args):
    if args['<private>']:
        private = binascii.unhexlify(args['<private>'])
        assert len(private) == 32
        return private
    else:
        return b'\0'*32


def get_recipients(args):
    if args['<recipients>']:
        recipients = []
        for recipient in args['<recipients>']:
            key = binascii.unhexlify(recipient)
            assert len(recipient) == 32
            recipients.append(key)
        return recipients
    else:
        # Without explicit recipients, just send to yourself.
        private = get_private(args)
        public = nacl.bindings.crypto_scalarmult_base(private)
        return [public]


def do_encrypt(args):
    message = args['--message']
    if message is None:
        encoded_message = sys.stdin.buffer.read()
    else:
        encoded_message = message.encode('utf8')
    sender = get_private(args)
    if args['--chunk']:
        chunk_size = int(args['--chunk'])
    else:
        chunk_size = 10**6
    recipients = get_recipients(args)
    output = encrypt(
        sender,
        recipients,
        encoded_message,
        chunk_size)
    sys.stdout.buffer.write(output)


def do_decrypt(args):
    message = sys.stdin.buffer.read()
    private = get_private(args)
    decoded_message = decrypt(message, private, debug=args['--debug'])
    sys.stdout.buffer.write(decoded_message)


def main():
    args = docopt.docopt(__doc__)
    if args['encrypt']:
        do_encrypt(args)
    else:
        do_decrypt(args)


if __name__ == '__main__':
    main()

# SillyBox

## Encryption

### Thinking
Our goals for encrypted messages:
- Message privacy and authenticity, to many recipients. Mallory can't read or
  modify the contents of a message. (Though Mallory can see the length.)
  - Even if Mallory is a recipient, she still can't modify the message for
    anyone else.
- Receiver privacy. Mallory can't tell who can read a message. (Though Mallory
  can see the number of recipients.)
  - Again this applies even if Mallory is a recipient.
  - However, senders have the option of publishing the recipients. That helps
    clients give instructions like, "To read this message, use [some other
    device]." Note that Mallory could modify the published recipients.
- Sender privacy. Mallory can't tell who wrote a message, even though
  recipients can see and verify the sender.
  - Senders who want to be anonymous even to the recipients, can use an
    ephemeral key instead of their usual public key.
- Repudiability. Recipients can forge messages that appear to be sent to them
  from any sender.
- Streaming. Recipients can produce decrypted bytes incrementally as a message
  comes in, without losing authenticity. (Though the message could be
  truncated.)

Our goals for the implementation:
- Use MessagePack for all the serialization.
- Use NaCl primitives for all the crypto: Box, SecretBox, SHA512, and
  HMAC-SHA512.

### Format
An encrypted message is a series of MessagePack objects:
- a header packet
- any number of non-empty payload packets
- an empty payload packet, marking the end of the message

The contents of the header packet array are:
- the format name string ("sillybox")
- the major version (1)
- the minor version (0)
- the mode (encryption, or attached/detached signing)
- an ephemeral public key (32 bytes)
- an array of **recipient sets**

A **recipient set** is also an array:
- the recipient public key (optional, either 32 bytes or null)
- the sender box
  - encrypted with the ephemeral private key
  - contains the 32-byte public sender key
- the keys box
  - encrypted with the sender's private key
  - contains a **key set**, as MessagePack bytes

A **key set** is yet another array:
- a 32-byte symmetric encryption key
  - This is the same for all recipients.
- a MAC group number
  - TODO: How do we fix the length of this?
- a 32-byte symmetric MAC key
  - This is shared by each recipient in the same MAC group. While every
    recipient could be in their own group, the intention is that a MAC group
    could represent a single person's collection of devices.
  - TODO: Omit MACing when there's only one MAC group?

The contents of a payload packet array are:
- an array of MACs
  - The index of each MAC in the array is the MAC group number from above.
  - The key is the symmetric MAC key for that group number.
  - The input to each MAC is the concatenation of two values:
    - the Poly1305 tag (the first 16 bytes) of the chunk box, below
    - the chunk index, as an 8-byte big-endian unsigned integer
- a chunk secret box
  - encrypted with the symmetric encryption key
- The packet contents, a MessagePack bin object. The maximum size of a bin
  object is about 4GB, but our default size will be 1MB.

An empty chunk signifies the end of the message.

## Example
A message with one recipient.
```
# header
[
  "sillybox",
  1,
  0,
  0,
  b"f5LbalfieMFlFalEPq2nYJi0InXd2TZRv/JDpMSCZCs=",
  [
    [
      null,
      b"EwaiG9lb78s/ZBhqss0PO7II2jW517fMeqNjyDRQqLJatnWUm+3DyXbPyINopLbE",
      b"wydMHuq5xI5GTYJF5MQUI9x2vgIMdJ2GK9KDVGSiJ1D6NuWfSs2dhGL7B+uFlcZi3irCqL2xOwVrVNzEI2o4VvFWeayLmpWmxeB42svFuRc1dn8uHOk="
    ]
  ]
]

# keys set (decrypted and unpacked)
[
  b"KYhzlhoUSBoZrtTcgKfKJo3tpTl0MkPHTKIwp0Xabj0=",
  0,
  b"np0Z7kdR8o8SLxnh0kb2AHZYgnSGTpU4oVGBTVbm2RY="
]

# packet 0
[
  [
    b"3xGnG2O9hgYV2BEQPBxbqvTTDQQeeCbW5ln5a9NoEr0="
  ],
  b"ailuqv38FS9zqIHRUvMpHaUpJzWa1ZPvZk8OzZzv4tECBwMJmwioGfb8P03vb62h2F8JNJlrgQ=="
]

# chunk 0 (decrypted)
'The Magic Words are Squeamish Ossifrage'

# packet 1
[
  [
    b"26oYynh1UeVV4xfo7RjpbCZ+bGa9miSM5qKR/KSpBlw="
  ],
  b"s1jqk6ILx7WsNZ2nJzyLEw=="
]

# chunk 1 (decrypted)
''
```

## Signing

### Format
Similar to encryption. A signed message is a series of packets, each of which
is a MessagePack array:
- a header packet
- any number of non-empty payload packets
- an empty payload packet, marking the end of the message

The contents of the header packet are:
- the format name
- the major version (1)
- the minor version (0)
- the mode number
- the signing public key

In detached mode, there is no payload, and the header contains an extra field:
- the detached NaCl sig of the SHA512 of the message
- TODO: concatenated with some other stuff?!

In attached mode, as in encryption mode, the header is followed by a number of
payload packets. Each payload packet contains:
- an attached NaCl sig of an ephemeral signing public key
  - signed by the sender for the first packet, or the previous ephemeral key
    for subsequent packets
  - TODO: concatenated with some other stuff?!
- an attached NaCl sig of the message chunk
  - signed by the ephemeral key above

An empty chunk signifies the end of the message.

### Attached
[
  "sillybox"
  1          # version
  1          # mode (attached signing)
  abc123...  # signer pk
]
[
  def456...  # first ephemeral pk carton, signed by signer
             # TODO: Should there be extra constants in this carton?
  c2c2c2...  # payload carton, signed by first ephemeral
]
...
[
  dadada...  # next ephemeral pk carton, signed by previous
  c2c2c2...  # next payload carton, signed by current
]
[
  5b5b5b...  # final ephemeral pk carton, signed by previous
  929292...  # empty carton, signed by current
]



[
  "sillybox"
  1          # version
  1          # mode (attached signing)
  abc123...  # signer pk
  Sig[signer](ephemeral_pk + SILLYBOX_SUFFIX)
]

[
  Sig[ephemeral](chunk + 64_bit_seqno)
]



### Detached
[
  "sillybox"
  1          # version
  2          # mode (detached signing)
  5d5d5d...  # sender
  afafaf...  # detached sig of SHA512 of payload
             # TODO: Should there be extra constants in this hash?
]


# TODO
think about take over attacks
- never sign anything we didn't generate
- how can we be careful about what we decrypt?

can we get a constant file prefix?
- what does `file` do?

use Major.Minor versioning (Fred's idea)
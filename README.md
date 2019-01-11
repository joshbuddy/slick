# Slick

**Easy to use secure file sending**

Slick makes it easy to send files or chat between two people. It is end-to-end encrypted, and uses HTTPS as its transport.

## Installation

Using python 3.7, run:

`pip install slick-app`

## Usage

To start slick, run `slick`. Running `/help` you'll get:

```
/list           -- show active friends and nearby people
/add  [subject] -- add a person
/talk [subject] -- talk to someone
/end            -- stop talking to someone
/quit           -- quit the program
/send           -- send a file
/get            -- get a file
/info
```

### Adding a friend

To add a friend, use the `/add` command. They will need to approve the request on their side by adding you back.

### Interacting with a friend

To interact with a specific friend, use `/talk [name]`

### Chatting

Once you're interacting with a friend, anything you type without a leading slash will be interpretted as chatting.

### Sending a file

To send a file, use `/send`

### Receiving a file

To receive a file, use `/get #`

## Design

At a high level, Slick allows encrypted communication between two parties. It accomplishes this by running two servers which are then made available over tor, the **certificate server** and the **talk server**. It distributes information on how to communicate via **multicast DNS**.

Slick advertizes for the following over **multicast DNS**:

1. A NaCL public key (https://nacl.cr.yp.to/box.html)
2. A SHA-256 digest of your HTTPS certificate
3. The onion address of the **certificate server**

The **certificate server** uses HTTP, and expects data encoded with the public key advertized over multicast DNS. Data sent to this server is used to request access to your **talk server**. The following data is sent:

1. The certificate of the user requesting access
2. The name of the user requesting access
3. The NaCL public key of the user requesting access

Once both parties mutually accept the certificates, then communication is performed over the **talk server**. The talk server is made available as an onion service. The address for this onion service is encoded in the certificate. HTTPS with mutual TLS is used to identity and authenticate users.

### Limitations

* Can't exchange keys without being on the same network
* An adversarial local network could MitM the key exchange, and unless certificate digests are compared, could compromise the key exchange

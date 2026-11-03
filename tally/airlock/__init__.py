"""The airlock: what runs inside the sandbox, on one untrusted file, with no network and no keys.

Everything here reads a PDF and writes a report and page images. Nothing here calls a model, opens a socket or reads
a secret, and nothing here should ever be imported by code that does.
"""

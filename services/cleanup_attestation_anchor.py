"""Build-time cleanup attestation trust anchor.

The Docker build overwrites this fail-closed placeholder with the SHA-256 of
the external launcher's Ed25519 public-key PEM. Runtime environment variables
may select a mounted key file, but cannot alter this accepted fingerprint.
"""

CLEANUP_ATTESTATION_PUBLIC_KEY_SHA256 = ""

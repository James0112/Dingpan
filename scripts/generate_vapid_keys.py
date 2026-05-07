from __future__ import annotations

import base64
import sys


def to_urlsafe_b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> int:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ModuleNotFoundError as exc:
        print("Missing dependency:", exc, file=sys.stderr)
        print("Run `pip install -r requirements.txt` inside the project virtualenv first.", file=sys.stderr)
        return 1

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_value = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    print("# Add these lines to your .env")
    print(f"VAPID_PUBLIC_KEY={to_urlsafe_b64(public_key)}")
    print(f"VAPID_PRIVATE_KEY={to_urlsafe_b64(private_value)}")
    print("VAPID_CLAIMS_EMAIL=your@email.com")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

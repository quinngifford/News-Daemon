"""Generate the VAPID keypair used for Web Push.

    .venv/bin/python tools/gen_vapid_keys.py --write-env    # recommended
    .venv/bin/python tools/gen_vapid_keys.py                # prints to stdout

Prefer --write-env: it appends the keys straight into deploy/ticker.env (0600)
instead of printing the private key to your terminal, where it would persist in
scrollback, shell logs, or a shared session transcript.

Writes var/vapid_private.pem (0600) either way.

Run once. Regenerating invalidates every existing push subscription, because the
browser ties a subscription to the key that created it — so if you rotate, every
device must re-subscribe or its pushes silently fail with 403.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "var"
PRIVATE_PEM = OUT_DIR / "vapid_private.pem"
ENV_FILE = ROOT / "deploy" / "ticker.env"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-env", action="store_true",
                    help="append the keys to deploy/ticker.env instead of "
                         "printing the private key to the terminal")
    ap.add_argument("--force", action="store_true",
                    help="rotate: overwrite an existing key (invalidates all "
                         "existing push subscriptions)")
    args = ap.parse_args()

    if PRIVATE_PEM.exists() and not args.force:
        print(f"refusing to overwrite {PRIVATE_PEM}")
        print("Existing subscriptions are bound to this key. Pass --force if "
              "you really intend to rotate.")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())

    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    PRIVATE_PEM.write_bytes(pem)
    PRIVATE_PEM.chmod(0o600)

    # Browsers want the raw uncompressed EC point (65 bytes, leading 0x04).
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    # pywebpush accepts a base64url DER private key string.
    private_der = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    pub_b64 = b64url(public_raw)
    priv_b64 = b64url(private_der)

    print(f"wrote {PRIVATE_PEM} (0600)")

    if args.write_env:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
        # Drop any previous VAPID lines so rotation does not leave two values,
        # where the shell would silently use whichever came last.
        kept = [
            ln for ln in existing.splitlines()
            if not ln.startswith(("TICKER_VAPID_PUBLIC_KEY=",
                                  "TICKER_VAPID_PRIVATE_KEY="))
        ]
        kept += [
            f'TICKER_VAPID_PUBLIC_KEY="{pub_b64}"',
            f'TICKER_VAPID_PRIVATE_KEY="{priv_b64}"',
        ]
        ENV_FILE.write_text("\n".join(kept).strip() + "\n", encoding="utf-8")
        ENV_FILE.chmod(0o600)
        print(f"wrote keys to {ENV_FILE} (0600) — private key NOT printed")
        print(f"public key (safe to share): {pub_b64}")
    else:
        print("\nAdd these to your environment (e.g. deploy/ticker.env):\n")
        print(f'TICKER_VAPID_PUBLIC_KEY="{pub_b64}"')
        print(f'TICKER_VAPID_PRIVATE_KEY="{priv_b64}"')
        print("\nTip: --write-env avoids printing the private key at all.")

    print("\nThe public key is served to the PWA at /api/vapid-public-key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

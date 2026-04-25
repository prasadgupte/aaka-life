"""tools/epc_qr.py — Generate EPC QR codes for SEPA bank transfers.

Produces a PNG QR code in the EPC/GiroCode standard (EPC069-12).
Scanned by N26, ING, Sparkasse, and most EU banking apps to pre-fill
recipient, IBAN, amount, and reference.

Dependencies: qrcode, pillow (install via `uv run --with qrcode --with pillow`)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def generate_epc_qr(
    name: str,
    iban: str,
    amount: float,
    reference: str,
    *,
    bic: str = "",
    currency: str = "EUR",
    output_path: str | Path | None = None,
) -> Path:
    """Generate an EPC QR code PNG for a SEPA transfer.

    Args:
        name:        Beneficiary name (max 70 chars).
        iban:        Beneficiary IBAN (no spaces).
        amount:      Transfer amount (0.01–999999999.99).
        reference:   Unstructured remittance info (max 140 chars).
        bic:         BIC/SWIFT (optional since SEPA 2014, but some apps want it).
        currency:    ISO 4217 currency code (default EUR).
        output_path: Where to save the PNG. Defaults to /tmp/epc-qr.png.

    Returns:
        Path to the saved PNG file.
    """
    try:
        import qrcode
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "qrcode", "pillow", "--quiet"],
        )
        import qrcode

    iban_clean = iban.replace(" ", "")
    name_clean = name[:70]
    ref_clean = reference[:140]

    epc_data = "\n".join([
        "BCD",                          # Service Tag
        "002",                          # Version
        "1",                            # Encoding (UTF-8)
        "SCT",                          # SEPA Credit Transfer
        bic,                            # BIC (optional)
        name_clean,                     # Beneficiary
        iban_clean,                     # IBAN
        f"{currency}{amount:.2f}",      # Amount
        "",                             # Purpose code
        "",                             # Structured reference
        ref_clean,                      # Unstructured reference
        "",                             # Information
    ])

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(epc_data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    out = Path(output_path) if output_path else Path("/tmp/epc-qr.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out))
    return out


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Generate EPC QR code for SEPA transfer")
    p.add_argument("--name", required=True, help="Beneficiary name")
    p.add_argument("--iban", required=True, help="Beneficiary IBAN")
    p.add_argument("--amount", required=True, type=float, help="Amount in EUR")
    p.add_argument("--reference", required=True, help="Payment reference")
    p.add_argument("--bic", default="", help="BIC/SWIFT (optional)")
    p.add_argument("--output", default="/tmp/epc-qr.png", help="Output PNG path")
    args = p.parse_args()

    path = generate_epc_qr(
        name=args.name,
        iban=args.iban,
        amount=args.amount,
        reference=args.reference,
        bic=args.bic,
        output_path=args.output,
    )
    print(f"QR saved to {path}")

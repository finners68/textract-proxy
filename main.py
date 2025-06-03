import os
import uuid
import base64
import logging
import time
from io import BytesIO
import fitz  # PyMuPDF
import boto3
import re

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# Environment variables
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET = os.getenv("S3_BUCKET")

if not all([AWS_ACCESS_KEY, AWS_SECRET_KEY, AWS_REGION, S3_BUCKET]):
    raise EnvironmentError("Missing one or more AWS environment variables.")

# AWS Clients
s3 = boto3.client("s3",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

textract = boto3.client("textract",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

# FastAPI app
app = FastAPI()

class ReceiptUpload(BaseModel):
    file: str  # base64-encoded file (PDF, PNG, or JPG)

@app.post("/process-receipt")
def process_receipt(data: ReceiptUpload):
    logger.info("\U0001F4E5 Received request to /process-receipt")

    try:
        b64 = data.file.strip()
        logger.info(f"\U0001F4E6 Base64 input starts: {b64[:40]}...")

        if "," in b64:
            b64 = b64.split(",", 1)[1]

        try:
            file_bytes = base64.b64decode(b64)
        except Exception:
            logger.exception("\u274C Base64 decoding failed")
            return JSONResponse(status_code=400, content={"error": "Invalid base64 input."})

        logger.info(f"\U0001F4C4 Decoded file size: {len(file_bytes)} bytes")
        logger.info(f"\U0001F4C4 First 10 bytes: {file_bytes[:10]}")

        # Detect file type
        content_type = None
        if file_bytes.startswith(b"%PDF"):
            content_type = "application/pdf"
        elif file_bytes.startswith(b"\x89PNG"):
            content_type = "image/png"
        elif file_bytes.startswith(b"\xff\xd8\xff"):
            content_type = "image/jpeg"
        else:
            logger.error("\u274C Unsupported file format")
            return JSONResponse(status_code=400, content={"error": "Unsupported file format. Only PDF, PNG, and JPEG are supported."})

        # Process PDF: convert to PNG
        if content_type == "application/pdf":
            logger.info("\U0001F5FC Rendering PDF as PNG image...")
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                page = doc.load_page(0)
                pix = page.get_pixmap(dpi=300)
                file_bytes = pix.tobytes("png")
                content_type = "image/png"
                doc.close()
            except Exception:
                logger.exception("\u274C Failed to convert PDF to image")
                return JSONResponse(status_code=500, content={"error": "PDF to image conversion failed"})

        # Upload file to S3
        filename = f"{uuid.uuid4()}"
        ext = ".png" if content_type == "image/png" else ".jpg"
        key = f"{filename}{ext}"
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=file_bytes, ContentType=content_type)

        logger.info("\U0001F9E0 Calling Textract (analyze_expense)...")
        try:
            response = textract.analyze_expense(
                Document={'S3Object': {'Bucket': S3_BUCKET, 'Name': key}}
            )

            raw_fields = {}
            for doc in response.get('ExpenseDocuments', []):
                for field in doc.get('SummaryFields', []):
                    type_text = field.get('Type', {}).get('Text', '').upper()
                    value = field.get('ValueDetection', {}).get('Text', '')
                    if type_text and value:
                        raw_fields[type_text] = value

            # Normalize for VAT compliance
            normalized = {
                "vendor_name": raw_fields.get("VENDOR_NAME") or raw_fields.get("SUPPLIER") or None,
                "total_amount": raw_fields.get("TOTAL") or raw_fields.get("INVOICE_TOTAL") or None,
                "subtotal_amount": raw_fields.get("SUBTOTAL") or raw_fields.get("AMOUNT_BEFORE_TAX") or None,
                "vat_amount": raw_fields.get("TAX") or raw_fields.get("VAT") or None,
                "vat_rate_percent": None,
                "currency": raw_fields.get("CURRENCY") or None,
                "invoice_date": raw_fields.get("INVOICE_RECEIPT_DATE") or raw_fields.get("DATE") or None
            }

            # Attempt to extract VAT % if embedded in any field
            for k, v in raw_fields.items():
                if re.search(r"(vat|tax).+\%", k, re.IGNORECASE) or re.search(r"\d+\.?\d*\s*%", v):
                    match = re.search(r"(\d+\.?\d*)\s*%", v)
                    if match:
                        normalized["vat_rate_percent"] = match.group(1)
                        break

            return {"fields": normalized}

        except textract.exceptions.UnsupportedDocumentException:
            logger.error("\u274C Textract rejected the document")
            return JSONResponse(status_code=400, content={"error": "Unsupported document format."})

    except Exception as e:
        logger.exception("Unhandled error in receipt processing")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/health")
def health():
    return {"status": "ok"}

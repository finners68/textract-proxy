import os
import boto3
import base64
import uuid
import logging
import time
from io import BytesIO
import fitz  # PyMuPDF

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from botocore.exceptions import ClientError, BotoCoreError
from typing import Dict

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# AWS configuration
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET = os.getenv("S3_BUCKET")

if not all([AWS_ACCESS_KEY, AWS_SECRET_KEY, AWS_REGION, S3_BUCKET]):
    raise EnvironmentError("Missing one or more AWS environment variables.")

# AWS clients
s3 = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

textract = boto3.client(
    "textract",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

# Input model
class ReceiptUpload(BaseModel):
    file: str  # base64-encoded PDF

@app.post("/process-receipt")
def process_receipt(data: ReceiptUpload):
    logger.info("📥 Received request to /process-receipt")

    try:
        b64 = data.file.strip()
        logger.info(f"📦 Base64 input starts: {b64[:40]}...")

        if "," in b64:
            b64 = b64.split(",", 1)[1]

        try:
            file_bytes = base64.b64decode(b64)
        except Exception:
            logger.exception("❌ Base64 decoding failed")
            return JSONResponse(status_code=400, content={"error": "Invalid base64 input."})

        logger.info(f"📄 Decoded file size: {len(file_bytes)} bytes")
        logger.info(f"📄 First 10 bytes: {file_bytes[:10]}")

        if not file_bytes.startswith(b"%PDF"):
            logger.error("❌ File does not start with %PDF")
            return JSONResponse(status_code=400, content={"error": "Uploaded file is not a valid PDF."})

        logger.info("🧼 Flattening PDF using PyMuPDF...")
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            flattened_pdf = BytesIO()
            doc.save(flattened_pdf)
            flattened_pdf.seek(0)
            file_bytes = flattened_pdf.read()
            doc.close()
        except Exception:
            logger.exception("❌ Failed to flatten PDF")
            return JSONResponse(status_code=500, content={"error": "PDF flattening failed"})

        logger.info(f"📄 Flattened PDF size: {len(file_bytes)} bytes")

        filename = f"{uuid.uuid4()}.pdf"
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=filename,
            Body=file_bytes,
            ContentType="application/pdf"
        )

        logger.info("🧠 Calling Textract (analyze_expense)...")
        response = textract.analyze_expense(
            Document={'S3Object': {'Bucket': S3_BUCKET, 'Name': filename}}
        )

        fields = extract_expense_fields(response)
        logger.info(f"✅ Extracted {len(fields)} fields from expense document")
        return {"fields": fields}

    except (ClientError, BotoCoreError) as e:
        logger.exception("AWS client error")
        return JSONResponse(status_code=502, content={"error": f"AWS error: {str(e)}"})
    except Exception as e:
        logger.exception("Unhandled error in receipt processing")
        return JSONResponse(status_code=500, content={"error": f"Internal error: {str(e)}"})

# Extract fields from analyze_expense
def extract_expense_fields(response) -> Dict[str, str]:
    fields = {}
    for doc in response.get("ExpenseDocuments", []):
        for field in doc.get("SummaryFields", []):
            key = field.get("Type", {}).get("Text", "").strip()
            value = field.get("ValueDetection", {}).get("Text", "").strip()
            if key and value:
                fields[key] = value
    return fields

@app.get("/health")
def health():
    return {"status": "ok"}

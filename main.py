import os
import boto3
import base64
import uuid
import logging
import time
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from botocore.exceptions import ClientError, BotoCoreError
from typing import Dict

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# AWS config
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
        except Exception as e:
            logger.exception("❌ Base64 decoding failed")
            return JSONResponse(status_code=400, content={"error": "Invalid base64 input."})

        logger.info(f"📄 Decoded file size: {len(file_bytes)} bytes")
        logger.info(f"📄 First 10 bytes: {file_bytes[:10]}")

        if not file_bytes.startswith(b"%PDF"):
            logger.error("❌ File does not start with %PDF")
            return JSONResponse(status_code=400, content={"error": "Uploaded file is not a valid PDF."})

        logger.info("✅ File passed PDF validation. Uploading to S3...")

        # TEMP: stop here to validate PDF and base64 only
        return JSONResponse(status_code=200, content={"message": "PDF looks good!"})

    except Exception as e:
        logger.exception("Unhandled error in receipt processing")
        return JSONResponse(status_code=500, content={"error": f"Internal error: {str(e)}"})

# Optional health route for testing
@app.get("/health")
def health():
    return {"status": "ok"}

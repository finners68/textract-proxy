import os
import base64
import uuid
import logging
import boto3
import fitz  # PyMuPDF
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

# Initialize FastAPI app
app = FastAPI()

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load AWS credentials from environment
AWS_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET_KEY")
AWS_REGION = os.environ.get("AWS_REGION")
S3_BUCKET = os.environ.get("S3_BUCKET")

if not all([AWS_ACCESS_KEY, AWS_SECRET_KEY, AWS_REGION, S3_BUCKET]):
    raise EnvironmentError("Missing one or more AWS environment variables.")

# Boto3 clients
s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION,
)

textract = boto3.client(
    "textract",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION,
)

@app.post("/process-receipt")
async def process_receipt(request: Request):
    try:
        logger.info("📥 Received request to /process-receipt")
        body = await request.json()

        if "file" not in body:
            return JSONResponse(status_code=400, content={"error": "Missing 'file' in request body."})

        base64_file = body["file"]
        original_filename = body.get("filename", f"receipt-{uuid.uuid4()}.pdf")
        logger.info(f"📦 Base64 input starts: {{base64_file[:50]}}...")

        try:
            file_bytes = base64.b64decode(base64_file)
        except Exception as e:
            logger.error("❌ Failed to decode base64")
            return JSONResponse(status_code=400, content={"error": "Base64 decoding failed."})

        logger.info(f"📄 Decoded file size: {len(file_bytes)} bytes")
        logger.info(f"📄 First 10 bytes: {file_bytes[:10]!r}")

        if not file_bytes.startswith(b"%PDF"):
            return JSONResponse(status_code=400, content={"error": "Unsupported file format. Must be PDF."})

        logger.info("🧼 Flattening PDF using PyMuPDF...")
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            new_pdf = fitz.open()
            for page in doc:
                new_pdf.insert_pdf(doc, from_page=page.number, to_page=page.number)
            flattened_bytes = new_pdf.tobytes()
            logger.info(f"📄 Flattened PDF size: {len(flattened_bytes)} bytes")
        except Exception as e:
            logger.error("❌ Failed to flatten PDF")
            return JSONResponse(status_code=500, content={"error": "Failed to flatten PDF."})

        # Upload to S3
        logger.info("✅ File passed PDF validation. Uploading to S3...")
        s3_key = original_filename  # Keep original name with .pdf
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=flattened_bytes,
            ContentType="application/pdf",
        )

        # Call Textract
        logger.info("🧠 Calling Textract (analyze_expense)...")
        try:
            response = textract.analyze_expense(
                Document={"S3Object": {"Bucket": S3_BUCKET, "Name": s3_key}}
            )
        except Exception as e:
            logger.error("❌ Textract rejected the document")
            return JSONResponse(status_code=400, content={"error": "Textract rejected the document."})

        return JSONResponse(status_code=200, content={"raw_fields": response})

    except Exception as e:
        logger.exception("🔥 Error during processing")
        return JSONResponse(status_code=500, content={"error": str(e)})

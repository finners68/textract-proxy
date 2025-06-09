import os
import io
import json
import base64
import logging
import fitz  # PyMuPDF
import boto3
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI()

# CORS for local/testing use
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# AWS Credentials
AWS_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET_KEY")
AWS_REGION = os.environ.get("AWS_REGION")
S3_BUCKET = os.environ.get("S3_BUCKET")

if not all([AWS_ACCESS_KEY, AWS_SECRET_KEY, AWS_REGION, S3_BUCKET]):
    raise EnvironmentError("Missing one or more AWS environment variables.")

# AWS Clients
s3 = boto3.client(
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
        if "file" not in body or "filename" not in body:
            return JSONResponse(status_code=400, content={"error": "Missing 'file' or 'filename' in request body."})

        base64_file = body["file"]
        original_filename = body["filename"]

        logger.info(f"📦 Base64 input starts: {base64_file[:30]}...")

        try:
            file_bytes = base64.b64decode(base64_file)
            logger.info(f"📄 Decoded file size: {len(file_bytes)} bytes")
            logger.info(f"📄 First 10 bytes: {file_bytes[:10]!r}")
        except Exception as e:
            logger.error("❌ Failed to decode base64", exc_info=True)
            return JSONResponse(status_code=400, content={"error": "Invalid base64 string."})

        # Flatten PDF to image
        try:
            logger.info("🧼 Flattening PDF using PyMuPDF...")
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            page = doc.load_page(0)  # only first page for now
            pix = page.get_pixmap(dpi=300)
            image_bytes = pix.tobytes("png")
            logger.info(f"📄 Flattened PDF size: {len(image_bytes)} bytes")
        except Exception as e:
            logger.error("❌ PDF flattening failed", exc_info=True)
            return JSONResponse(status_code=400, content={"error": "Could not flatten PDF."})

        # Keep original name prefix, append UUID, enforce .png
        filename_base = os.path.splitext(original_filename)[0]
        filename = f"{filename_base}-{uuid.uuid4()}.png"

        # Upload image to S3
        try:
            logger.info(f"☁️ Uploading image to S3 as: {filename}")
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=filename,
                Body=image_bytes,
                ContentType="image/png",
            )
        except Exception as e:
            logger.error("❌ Failed to upload to S3", exc_info=True)
            return JSONResponse(status_code=500, content={"error": "S3 upload failed."})

        # Call Textract AnalyzeExpense
        try:
            logger.info("🧠 Calling Textract (analyze_expense)...")
            response = textract.analyze_expense(
                Document={"S3Object": {"Bucket": S3_BUCKET, "Name": filename}}
            )
        except Exception as e:
            logger.error("❌ Textract rejected the document", exc_info=True)
            return JSONResponse(status_code=400, content={"error": "Unsupported document format."})

        # Extract raw fields
        raw_fields = {}
        for doc in response.get("ExpenseDocuments", []):
            for field in doc.get("SummaryFields", []):
                if "Type" in field and "ValueDetection" in field:
                    field_name = field["Type"].get("Text", "").strip().upper().replace(" ", "_")
                    field_value = field["ValueDetection"].get("Text", "").strip()
                    if field_name and field_value:
                        raw_fields[field_name] = field_value

        # Extract line items
        line_items = []
        for doc in response.get("ExpenseDocuments", []):
            for group in doc.get("LineItemGroups", []):
                for item in group.get("LineItems", []):
                    fields = {
                        f['Type']['Text'].lower(): f['ValueDetection']['Text']
                        for f in item.get('LineItemExpenseFields', [])
                        if 'Type' in f and 'ValueDetection' in f
                    }
                    if fields:
                        line_items.append(fields)

        return JSONResponse(status_code=200, content={
            "raw_fields": raw_fields,
            "line_items": line_items
        })

    except Exception as e:
        logger.error("🔥 Unhandled exception during /process-receipt", exc_info=True)
        return JSONResponse(status_code=500, content={"error": "Internal server error."})

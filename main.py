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
    file: str  # base64-encoded PDF

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

        if not file_bytes.startswith(b"%PDF"):
            logger.error("\u274C File does not start with %PDF")
            return JSONResponse(status_code=400, content={"error": "Uploaded file is not a valid PDF."})

        logger.info("\U0001F9FC Flattening PDF using PyMuPDF...")
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            flattened_pdf = BytesIO()
            doc.save(flattened_pdf)
            flattened_pdf.seek(0)
            file_bytes = flattened_pdf.read()
            doc.close()
        except Exception:
            logger.exception("\u274C Failed to flatten PDF")
            return JSONResponse(status_code=500, content={"error": "PDF flattening failed"})

        logger.info(f"\U0001F4C4 Flattened PDF size: {len(file_bytes)} bytes")

        filename = f"{uuid.uuid4()}.pdf"
        s3.put_object(Bucket=S3_BUCKET, Key=filename, Body=file_bytes, ContentType="application/pdf")

        logger.info("\U0001F9E0 Calling Textract async analyze_document...")
        start_response = textract.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": S3_BUCKET, "Name": filename}},
            FeatureTypes=["FORMS"]
        )

        job_id = start_response["JobId"]
        logger.info(f"\U0001F4CB Textract Job ID: {job_id}")

        # Poll for job completion
        while True:
            result = textract.get_document_analysis(JobId=job_id)
            status = result["JobStatus"]
            if status == "SUCCEEDED":
                break
            elif status == "FAILED":
                return JSONResponse(status_code=500, content={"error": "Textract analysis failed."})
            time.sleep(2)

        fields = extract_vat_fields(result["Blocks"])
        return {"fields": fields}

    except Exception as e:
        logger.exception("Unhandled error in receipt processing")
        return JSONResponse(status_code=500, content={"error": str(e)})


def extract_vat_fields(blocks):
    block_map = {b['Id']: b for b in blocks}
    key_map = {}
    value_map = {}
    results = {}
    vat_number = None
    total = None
    tax = None

    for block in blocks:
        if block['BlockType'] == 'KEY_VALUE_SET':
            if 'KEY' in block.get('EntityTypes', []):
                key_map[block['Id']] = block
            else:
                value_map[block['Id']] = block

    for key_id, key_block in key_map.items():
        key_text = get_text(key_block, block_map).lower()
        val_id = key_block.get("Relationships", [{}])[0].get("Ids", [None])[0]
        val_text = get_text(value_map.get(val_id, {}), block_map) if val_id else ""

        if "total" in key_text:
            total = parse_currency(val_text)
            results['TOTAL'] = total
        elif "tax" in key_text or "vat" in key_text:
            tax = parse_currency(val_text)
            results['VAT_AMOUNT'] = tax
        elif "date" in key_text:
            results['DATE'] = val_text
        elif "invoice" in key_text:
            results['INVOICE_NO'] = val_text
        elif "vendor" in key_text or "seller" in key_text:
            results['VENDOR'] = val_text

        if not vat_number:
            match = re.search(r'\bGB\d{9}\b', val_text)
            if match:
                vat_number = match.group()

    if vat_number:
        results['VAT_NUMBER'] = vat_number

    if tax and total and total > 0:
        vat_rate = round((tax / total) * 100, 2)
        results['VAT_RATE'] = f"{vat_rate}%"

    return results


def get_text(block, block_map):
    if not block or "Relationships" not in block:
        return ""
    words = []
    for rel in block["Relationships"]:
        if rel["Type"] == "CHILD":
            for id in rel["Ids"]:
                word = block_map.get(id)
                if word and word.get("BlockType") == "WORD":
                    words.append(word.get("Text", ""))
    return " ".join(words)


def parse_currency(value):
    value = value.replace(",", "").replace("£", "").strip()
    try:
        return float(re.findall(r"\d+\.\d{2}", value)[0])
    except:
        return None


@app.get("/health")
def health():
    return {"status": "ok"}

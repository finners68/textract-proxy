import os
import io
import json
import base64
import logging
import tempfile
import fitz  # PyMuPDF
import boto3
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AWS_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET_KEY")
AWS_REGION = os.environ.get("AWS_REGION")
S3_BUCKET = os.environ.get("S3_BUCKET")
FLATTEN_DPI = int(os.environ.get("PDF_FLATTEN_DPI", "200"))  # was 300. make it tunable

if not all([AWS_ACCESS_KEY, AWS_SECRET_KEY, AWS_REGION, S3_BUCKET]):
    raise EnvironmentError("Missing one or more AWS environment variables.")

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
    pdf_path = None
    png_path = None
    try:
        logger.info("📥 Received request to /process-receipt")
        body = await request.json()
        if "file" not in body or "filename" not in body:
            return JSONResponse(status_code=400, content={"error": "Missing 'file' or 'filename' in request body."})

        base64_file = body["file"]
        original_filename = body["filename"]

        # Strip data URL prefix if present
        if base64_file.startswith("data:"):
            base64_file = base64_file.split(",", 1)[1]

        # 1) Stream-decode base64 to a temp PDF on disk
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
                pdf_path = tmp_pdf.name
                src = io.BytesIO(base64_file.encode("ascii"))
                base64.decode(src, tmp_pdf)  # no giant in-memory bytes
            pdf_size = os.path.getsize(pdf_path)
            logger.info(f"📄 Decoded PDF size on disk: {pdf_size} bytes")
        except Exception:
            logger.error("❌ Failed to decode base64 to PDF", exc_info=True)
            return JSONResponse(status_code=400, content={"error": "Invalid base64 string."})

        # 2) Flatten first page to PNG written directly to disk
        try:
            logger.info(f"🧼 Flattening PDF with PyMuPDF at {FLATTEN_DPI} DPI...")
            doc = fitz.open(pdf_path)
            page = doc.load_page(0)
            pix = page.get_pixmap(dpi=FLATTEN_DPI, alpha=False)  # avoid RGBA
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
                png_path = tmp_png.name
            pix.save(png_path)  # write file, do not build image_bytes
            doc.close()
            png_size = os.path.getsize(png_path)
            logger.info(f"🖼️ PNG on disk size: {png_size} bytes")
        except Exception:
            logger.error("❌ PDF flattening failed", exc_info=True)
            return JSONResponse(status_code=400, content={"error": "Could not flatten PDF."})

        # 3) Upload PNG by streaming from disk
        try:
            filename_base = os.path.splitext(original_filename)[0]
            filename = f"{filename_base}-{uuid.uuid4()}.png"
            logger.info(f"☁️ Uploading image to S3 as: {filename}")
            s3.upload_file(
                Filename=png_path,
                Bucket=S3_BUCKET,
                Key=filename,
                ExtraArgs={"ContentType": "image/png"},
            )
        except Exception:
            logger.error("❌ Failed to upload to S3", exc_info=True)
            return JSONResponse(status_code=500, content={"error": "S3 upload failed."})

        # 4) Call Textract AnalyzeExpense unchanged
        try:
            logger.info("🧠 Calling Textract (analyze_expense)...")
            response = textract.analyze_expense(
                Document={"S3Object": {"Bucket": S3_BUCKET, "Name": filename}}
            )
        except Exception:
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

    except Exception:
        logger.error("🔥 Unhandled exception during /process-receipt", exc_info=True)
        return JSONResponse(status_code=500, content={"error": "Internal server error."})
    finally:
        # Cleanup temp files
        for p in (pdf_path, png_path):
            try:
                if p and os.path.exists(p):
                    os.unlink(p)
            except Exception:
                pass

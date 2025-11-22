import os
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

# AWS credentials
AWS_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET_KEY")
AWS_REGION = os.environ.get("AWS_REGION")
S3_BUCKET = os.environ.get("S3_BUCKET")

if not all([AWS_ACCESS_KEY, AWS_SECRET_KEY, AWS_REGION, S3_BUCKET]):
    raise EnvironmentError("Missing one or more AWS environment variables.")

# AWS clients
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


# Detect file type by header bytes
def sniff_kind(header: bytes):
    h = header[:64]
    if h.startswith(b"%PDF-"):
        return ("pdf", ".pdf", "application/pdf")
    if h.startswith(b"\xFF\xD8\xFF"):
        return ("jpeg", ".jpg", "image/jpeg")
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("png", ".png", "image/png")
    if h.startswith(b"II*\x00") or h.startswith(b"MM\x00*"):
        return ("tiff", ".tiff", "image/tiff")
    if b"ftypheic" in h or b"ftypheif" in h or b"ftypmif1" in h:
        return ("heic", ".heic", "image/heic")
    return (None, None, None)


# Convert uploaded file to a list of image bytes, one per page
async def pdf_or_image_to_images(request: Request):
    body = await request.json()

    if "file" not in body or "filename" not in body:
        return None, JSONResponse(
            status_code=400,
            content={"error": "Missing file or filename in request body."},
        )

    base64_file = body["file"]
    original_filename = body["filename"]

    if isinstance(base64_file, str) and base64_file.startswith("data:"):
        base64_file = base64_file.split(",", 1)[1]

    try:
        file_bytes = base64.b64decode(base64_file)
    except Exception:
        return None, JSONResponse(status_code=400, content={"error": "Invalid base64 string."})

    kind, ext, content_type = sniff_kind(file_bytes[:64])

    if kind is None:
        return None, JSONResponse(status_code=415, content={"error": "Unsupported file type."})
    if kind == "heic":
        return None, JSONResponse(status_code=415, content={"error": "HEIC not supported."})

    # If it's an image, return as one page list
    if kind in ("jpeg", "png", "tiff"):
        return [
            {
                "bytes": file_bytes,
                "ext": ext,
                "content_type": content_type
            }
        ], None

    # If it's a PDF, flatten all pages
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        pages = []

        for i in range(len(doc)):
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=150, alpha=False)
            image_bytes = pix.tobytes("png")

            pages.append({
                "bytes": image_bytes,
                "ext": ".png",
                "content_type": "image/png"
            })

        doc.close()
        return pages, None

    except Exception:
        return None, JSONResponse(status_code=400, content={"error": "Could not flatten PDF."})


# Upload a single image to S3
def upload_to_s3(image_bytes, ext, content_type, base_name):
    filename = f"{base_name}-{uuid.uuid4()}{ext}"

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=filename,
        Body=image_bytes,
        ContentType=content_type,
    )

    return filename


# -------------------------------------------------------------------
# 1. AnalyzeExpense (receipt) endpoint
# -------------------------------------------------------------------
@app.post("/process-receipt")
async def process_receipt(request: Request):
    try:
        pages, error = await pdf_or_image_to_images(request)
        if error:
            return error

        # Amazon AnalyzeExpense accepts only one page
        first_page = pages[0]
        base_name = "receipt"

        try:
            filename = upload_to_s3(
                first_page["bytes"],
                first_page["ext"],
                first_page["content_type"],
                base_name
            )
        except Exception:
            return JSONResponse(status_code=500, content={"error": "S3 upload failed."})

        try:
            response = textract.analyze_expense(
                Document={"S3Object": {"Bucket": S3_BUCKET, "Name": filename}}
            )
        except Exception:
            return JSONResponse(status_code=400, content={"error": "AnalyzeExpense failed."})

        raw_fields = {}
        line_items = []

        for doc in response.get("ExpenseDocuments", []):
            for field in doc.get("SummaryFields", []):
                if "Type" in field and "ValueDetection" in field:
                    name = field["Type"]["Text"].strip().upper().replace(" ", "_")
                    value = field["ValueDetection"]["Text"].strip()
                    if name and value:
                        raw_fields[name] = value

            for group in doc.get("LineItemGroups", []):
                for item in group.get("LineItems", []):
                    fields = {
                        f["Type"]["Text"].lower(): f["ValueDetection"]["Text"]
                        for f in item.get("LineItemExpenseFields", [])
                        if "Type" in f and "ValueDetection" in f
                    }
                    if fields:
                        line_items.append(fields)

        return JSONResponse(
            status_code=200,
            content={"raw_fields": raw_fields, "line_items": line_items},
        )

    except Exception:
        return JSONResponse(status_code=500, content={"error": "Internal server error."})


# -------------------------------------------------------------------
# 2. Regular OCR endpoint (multi page supported)
# -------------------------------------------------------------------
@app.post("/process-ocr")
async def process_ocr(request: Request):
    try:
        pages, error = await pdf_or_image_to_images(request)
        if error:
            return error

        all_lines = []

        for page_index, page_data in enumerate(pages):
            base_name = f"ocr-page-{page_index}"

            try:
                filename = upload_to_s3(
                    page_data["bytes"],
                    page_data["ext"],
                    page_data["content_type"],
                    base_name
                )
            except Exception:
                return JSONResponse(status_code=500, content={"error": "S3 upload failed."})

            try:
                response = textract.detect_document_text(
                    Document={"S3Object": {"Bucket": S3_BUCKET, "Name": filename}}
                )
            except Exception:
                return JSONResponse(status_code=400, content={"error": "Textract OCR failed."})

            for block in response.get("Blocks", []):
                if block.get("BlockType") == "LINE":
                    all_lines.append(block.get("Text", ""))

        return JSONResponse(
            status_code=200,
            content={"text_lines": all_lines},
        )

    except Exception:
        return JSONResponse(status_code=500, content={"error": "Internal server error."})

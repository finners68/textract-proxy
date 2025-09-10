import os, uuid, logging, base64, tempfile, mimetypes
import boto3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from botocore.exceptions import BotoCoreError, ClientError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("textract-proxy")
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

AWS_ACCESS_KEY = os.environ["AWS_ACCESS_KEY"]
AWS_SECRET_KEY = os.environ["AWS_SECRET_KEY"]
AWS_REGION     = os.environ["AWS_REGION"]
S3_BUCKET      = os.environ["S3_BUCKET"]

s3 = boto3.client("s3",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION,
)
textract = boto3.client("textract",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION,
)

SUPPORTED_MIME = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
}

MAX_BYTES = 25 * 1024 * 1024  # 25 MB guardrail

def infer_ext(content_type: str, filename: str | None) -> str:
    if content_type in SUPPORTED_MIME:
        return SUPPORTED_MIME[content_type]
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext in [".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"]:
            return ext
    return ""

@app.post("/process-receipt")
async def process_receipt(req: Request):
    try:
        body = await req.json()
        b64 = body.get("file")
        filename = body.get("filename")  # optional
        content_type = body.get("contentType")  # strongly recommended

        if not b64:
            return JSONResponse(status_code=400, content={"error": "file (base64) is required"})

        ext = infer_ext(content_type or "", filename)
        if not ext:
            return JSONResponse(
                status_code=415,
                content={"error": "Unsupported file type. Allowed: PDF, JPEG, PNG, TIFF"}
            )
        key = f"receipts/{uuid.uuid4()}{ext}"
        ct = content_type or mimetypes.types_map.get(ext, "application/octet-stream")

        # Stream-decode base64 into a SpooledTemporaryFile
        # This uses memory only up to 'max_size', then spills to disk
        with tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024) as tmp:  # 4 MB RAM cap
            # Avoid loading entire string into memory if client sent data URL
            # Support both "data:...;base64,XXXX" and plain base64
            if "," in b64 and b64.strip().startswith("data:"):
                b64 = b64.split(",", 1)[1]

            # Decode in chunks
            # base64.decode expects file-like objects
            import io as _io, base64 as _b64
            src = _io.BytesIO(b64.encode("ascii"))
            _b64.decode(src, tmp)  # writes decoded bytes to tmp without expanding in RAM

            # size guard after decode
            tmp.seek(0, os.SEEK_END)
            size = tmp.tell()
            if size > MAX_BYTES:
                return JSONResponse(status_code=413, content={"error": f"File too large: {size} bytes"})

            tmp.seek(0)

            # Upload to S3 without reading into memory
            s3.upload_fileobj(tmp, S3_BUCKET, key, ExtraArgs={"ContentType": ct})

        # Call Textract directly on S3Object
        out = textract.analyze_expense(
            Document={"S3Object": {"Bucket": S3_BUCKET, "Name": key}}
        )

        # Parse results
        raw_fields = {}
        for doc in out.get("ExpenseDocuments", []):
            for field in doc.get("SummaryFields", []):
                t = field.get("Type", {}).get("Text", "")
                v = field.get("ValueDetection", {}).get("Text", "")
                name = t.strip().upper().replace(" ", "_")
                if name and v:
                    raw_fields[name] = v.strip()

        line_items = []
        for doc in out.get("ExpenseDocuments", []):
            for group in doc.get("LineItemGroups", []):
                for item in group.get("LineItems", []):
                    fields = {
                        f["Type"]["Text"].lower(): f["ValueDetection"]["Text"]
                        for f in item.get("LineItemExpenseFields", [])
                        if "Type" in f and "ValueDetection" in f
                    }
                    if fields:
                        line_items.append(fields)

        return {
            "raw_fields": raw_fields,
            "line_items": line_items,
            "meta": {"bucket": S3_BUCKET, "key": key, "content_type": ct, "size_bytes": size},
        }

    except (BotoCoreError, ClientError) as e:
        log.exception("AWS error")
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        log.exception("Unhandled")
        return JSONResponse(status_code=500, content={"error": "Internal server error"})
